"""
extract_points.py

Step 2 of the SOGRAIN 2.0 pipeline.

For each downloaded PIXC granule, extracts all pixel-cloud points that fall
within `buffer_m` metres of a canal centreline and writes them to per-canal
Parquet files.

Performance notes:
  1. Coarse lat/lon bbox pre-filter (numpy) applied BEFORE UTM projection,
     so only nearby pixels are ever reprojected.
  2. Combined canal bounding boxes cached per unique set of relevant grain_ids
     to avoid redundant unary_union + buffer computations.
  3. netCDF4 used for direct array reads instead of xarray (no lazy-graph overhead).
  4. Per-canal numpy bbox filter applied before the expensive Shapely distance call.
  5. gc.collect() called every GC_INTERVAL granules instead of every granule.

Output: one Parquet file per canal under
  {run_base_dir}/{region}_from_{run_start}_to_{run_end}/
      extracted_points/chunk_{canal_start}_{canal_end}/{grain_id}.parquet
"""
from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

import geopandas as gpd
import netCDF4 as ncf
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from pyproj import CRS, Transformer
from shapely import points, distance, line_locate_point
from shapely.ops import unary_union

GC_INTERVAL = 50   # run gc.collect() every N granules


def parse_args():
    p = argparse.ArgumentParser(description="Extract PIXC points near canal centrelines.")
    p.add_argument("--config",       required=True, help="Path to config.yaml")
    p.add_argument("--region",       required=True, help="Region name")
    p.add_argument("--run_start",    type=int, required=True, help="Overall run canal start index")
    p.add_argument("--run_end",      type=int, required=True, help="Overall run canal end index")
    p.add_argument("--canal_start",  type=int, required=True, help="This chunk's canal start index")
    p.add_argument("--canal_end",    type=int, required=True, help="This chunk's canal end index")
    p.add_argument("--chunk_id",     required=True, help="Chunk identifier string (e.g. '0_300')")
    return p.parse_args()


def get_utm_crs_from_lonlat(lon: float, lat: float) -> CRS:
    """Return the UTM CRS for a given longitude/latitude."""
    zone = int((lon + 180) // 6) + 1
    return CRS.from_epsg(32600 + zone if lat >= 0 else 32700 + zone)


def read_pixc(path: Path) -> dict | None:
    """
    Read pixel_cloud variables from a PIXC .nc file using netCDF4 directly.
    Returns a dict of flat numpy arrays, or None if the file cannot be read.
    """
    try:
        with ncf.Dataset(path) as ds:
            pc = ds["pixel_cloud"]
            return {
                "latitude":                             pc["latitude"][:].filled(np.nan).ravel(),
                "longitude":                            pc["longitude"][:].filled(np.nan).ravel(),
                "height":                               pc["height"][:].filled(np.nan).ravel(),
                "geoid":                                pc["geoid"][:].filled(np.nan).ravel(),
                "classification":                       pc["classification"][:].filled(0).astype(np.int8).ravel(),
                "water_frac":                           pc["water_frac"][:].filled(np.nan).ravel(),
                "water_frac_uncert":                    pc["water_frac_uncert"][:].filled(np.nan).ravel(),
                "cross_track":                          pc["cross_track"][:].filled(np.nan).ravel(),
                "interferogram_qual":                   pc["interferogram_qual"][:].filled(255).ravel(),
                "classification_qual":                  pc["classification_qual"][:].filled(255).ravel(),
                "geolocation_qual":                     pc["geolocation_qual"][:].filled(255).ravel(),
                "ancillary_surface_classification_flag":
                    pc["ancillary_surface_classification_flag"][:].filled(255).ravel(),
                "sig0_qual":                            pc["sig0_qual"][:].filled(255).ravel(),
            }
    except OSError:
        print(f"[WARN] Corrupted / unreadable PIXC file skipped: {path}")
        return None


class CanalBboxCache:
    """
    Caches the combined buffered bounding box (minx, miny, maxx, maxy) for each
    unique frozenset of grain_ids to avoid recomputing unary_union + buffer + bounds.
    """
    def __init__(self, canal_lookup: dict, buffer_m: float):
        self._lookup = canal_lookup
        self._buffer = buffer_m
        self._cache: dict = {}

    def get(self, grain_ids: list[str]):
        key = frozenset(grain_ids)
        if key not in self._cache:
            combined = unary_union([self._lookup[g] for g in grain_ids])
            buffered = combined.buffer(self._buffer)
            self._cache[key] = buffered.bounds   # (minx, miny, maxx, maxy)
        return self._cache[key]


def main():
    args   = parse_args()
    config = yaml.safe_load(open(args.config))

    region   = args.region
    chunk_id = args.chunk_id

    run_root      = Path(config["run_base_dir"]) / f"{region}_from_{args.run_start}_to_{args.run_end}"
    canals_path   = Path(config["base_grain_dir"]) / f"{region}_GRAIN_v.1.0.parquet"
    planning_json = run_root / config["planning_dir"] / "pixc_to_grains.json"
    download_dir  = run_root / config["download_dir"]
    out_dir       = run_root / config["extracted_points_dir"] / f"chunk_{chunk_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    buffer_m = float(config["buffer_m"])

    # Load canal geometries for this chunk (subset of the full region)
    canals = gpd.read_parquet(canals_path)
    if canals.crs is None:
        raise RuntimeError("Canals file has no CRS — check your GRAIN parquet file.")
    if canals.crs.to_epsg() != 4326:
        canals = canals.to_crs(4326)

    canals = canals.sort_values("grain_id").iloc[args.canal_start:args.canal_end]
    print(f"[INFO] Chunk {chunk_id}: canals {args.canal_start}:{args.canal_end}")

    centroid   = canals.geometry.union_all().centroid
    target_crs = get_utm_crs_from_lonlat(centroid.x, centroid.y)
    canals     = canals.to_crs(target_crs)
    print(f"[INFO] Using CRS: {target_crs}")

    canal_lookup  = {str(r["grain_id"]): r.geometry for _, r in canals.iterrows()}
    canal_ids_set = set(canal_lookup.keys())

    # Pre-compute WGS84 lon/lat bounds per canal for the coarse pre-filter
    canals_wgs84 = canals.to_crs(4326)
    canal_lonlat_bounds = {
        str(r["grain_id"]): r.geometry.bounds   # (minx, miny, maxx, maxy) in degrees
        for _, r in canals_wgs84.iterrows()
    }

    with open(planning_json) as f:
        pixc_to_grains = json.load(f)

    transformer = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    bbox_cache  = CanalBboxCache(canal_lookup, buffer_m)
    writers: dict = {}

    pixc_files = sorted(download_dir.glob("*.nc"))
    n_total    = len(pixc_files)

    for granule_idx, pixc_path in enumerate(pixc_files, 1):
        granule_name = pixc_path.stem

        if granule_name not in pixc_to_grains:
            continue

        relevant_grains = [
            str(g) for g in pixc_to_grains[granule_name]
            if str(g) in canal_ids_set
        ]
        if not relevant_grains:
            continue

        print(f"[INFO] {chunk_id} → {granule_name}  ({granule_idx}/{n_total})")

        data = read_pixc(pixc_path)
        if data is None:
            continue

        lat = data["latitude"]
        lon = data["longitude"]

        valid = np.isfinite(lat) & np.isfinite(lon)
        if not valid.any():
            continue

        lat = lat[valid]
        lon = lon[valid]

        # Coarse lon/lat bbox filter before the expensive UTM projection
        buf_deg = max(buffer_m / 100_000, 0.005)
        all_bounds  = [canal_lonlat_bounds[g] for g in relevant_grains]
        coarse_minx = min(b[0] for b in all_bounds) - buf_deg
        coarse_miny = min(b[1] for b in all_bounds) - buf_deg
        coarse_maxx = max(b[2] for b in all_bounds) + buf_deg
        coarse_maxy = max(b[3] for b in all_bounds) + buf_deg

        coarse_mask = (
            (lon >= coarse_minx) & (lon <= coarse_maxx) &
            (lat >= coarse_miny) & (lat <= coarse_maxy)
        )
        if not coarse_mask.any():
            continue

        lat_c = lat[coarse_mask]
        lon_c = lon[coarse_mask]

        def _slice(key):
            return data[key][valid][coarse_mask]

        height              = _slice("height")
        geoid               = _slice("geoid")
        classification      = _slice("classification")
        water_frac          = _slice("water_frac")
        water_frac_uncert   = _slice("water_frac_uncert")
        cross_track         = _slice("cross_track")
        interferogram_qual  = _slice("interferogram_qual")
        classification_qual = _slice("classification_qual")
        geolocation_qual    = _slice("geolocation_qual")
        ancillary_flag      = _slice("ancillary_surface_classification_flag")
        sig0_qual           = _slice("sig0_qual")

        xs, ys = transformer.transform(lon_c, lat_c)

        # Combined-canal bbox filter in UTM (avoids per-canal Shapely calls for distant points)
        minx, miny, maxx, maxy = bbox_cache.get(relevant_grains)
        mask_bbox = (
            (xs >= minx) & (xs <= maxx) &
            (ys >= miny) & (ys <= maxy)
        )
        if not mask_bbox.any():
            continue

        xs_sub = xs[mask_bbox]
        ys_sub = ys[mask_bbox]

        for grain_id in relevant_grains:
            canal_geom = canal_lookup[grain_id]

            # Per-canal bbox filter before the expensive Shapely distance call
            g_minx, g_miny, g_maxx, g_maxy = canal_geom.bounds
            grain_bbox_mask = (
                (xs_sub >= g_minx - buffer_m) & (xs_sub <= g_maxx + buffer_m) &
                (ys_sub >= g_miny - buffer_m) & (ys_sub <= g_maxy + buffer_m)
            )
            if not grain_bbox_mask.any():
                continue

            xs_g  = xs_sub[grain_bbox_mask]
            ys_g  = ys_sub[grain_bbox_mask]
            pts_g = points(xs_g, ys_g)

            dist_all    = distance(pts_g, canal_geom)
            mask_buffer = dist_all <= buffer_m
            if not mask_buffer.any():
                continue

            sub_idx  = np.where(mask_bbox)[0][grain_bbox_mask][mask_buffer]
            pts_keep = points(xs_g[mask_buffer], ys_g[mask_buffer])
            d_vals   = line_locate_point(canal_geom, pts_keep)

            df_out = pd.DataFrame({
                "grain_id":                                grain_id,
                "pixc_granule_id":                         granule_name,
                "x":                                       xs_g[mask_buffer],
                "y":                                       ys_g[mask_buffer],
                "d_m":                                     d_vals,
                "dist_to_canal_m":                         dist_all[mask_buffer],
                "height":                                  height[mask_bbox][grain_bbox_mask][mask_buffer],
                "geoid":                                   geoid[mask_bbox][grain_bbox_mask][mask_buffer],
                "classification":                          classification[mask_bbox][grain_bbox_mask][mask_buffer],
                "water_frac":                              water_frac[mask_bbox][grain_bbox_mask][mask_buffer],
                "water_frac_uncert":                       water_frac_uncert[mask_bbox][grain_bbox_mask][mask_buffer],
                "cross_track":                             cross_track[mask_bbox][grain_bbox_mask][mask_buffer],
                "interferogram_qual":                      interferogram_qual[mask_bbox][grain_bbox_mask][mask_buffer],
                "classification_qual":                     classification_qual[mask_bbox][grain_bbox_mask][mask_buffer],
                "geolocation_qual":                        geolocation_qual[mask_bbox][grain_bbox_mask][mask_buffer],
                "ancillary_surface_classification_flag":   ancillary_flag[mask_bbox][grain_bbox_mask][mask_buffer],
                "sig0_qual":                               sig0_qual[mask_bbox][grain_bbox_mask][mask_buffer],
            })

            table    = pa.Table.from_pandas(df_out, preserve_index=False)
            out_path = out_dir / f"{grain_id}.parquet"

            if grain_id not in writers:
                writers[grain_id] = pq.ParquetWriter(
                    out_path, table.schema,
                    compression="snappy",
                    use_dictionary=True,
                )
            writers[grain_id].write_table(table)
            del df_out

        if granule_idx % GC_INTERVAL == 0:
            gc.collect()

    for writer in writers.values():
        writer.close()

    (out_dir / "extraction_complete.flag").write_text("done\n")
    print(f"[INFO] Chunk {chunk_id} completed.")


if __name__ == "__main__":
    main()
