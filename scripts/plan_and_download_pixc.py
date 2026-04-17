"""
plan_and_download_pixc.py

Step 1 of the SOGRAIN 2.0 pipeline.

Searches the NASA EarthData SWOT PIXC archive for granules that overlap
the canal segments in the specified region and downloads them. Only the
pixel_cloud variables needed downstream are fetched via OPeNDAP subsetting;
granules without an OPeNDAP endpoint fall back to a full download.

Outputs (written to {run_base_dir}/{region}_from_{start}_to_{end}/pixc_planning/):
  pixc_to_grains.json   — mapping of granule name → list of overlapping grain_ids
  unique_pixc_granules.txt — one granule name per line (for reference)
"""

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict

import geopandas as gpd
import earthaccess
import requests
from shapely.geometry import box, Polygon
from shapely.strtree import STRtree
import yaml


# PIXC variables downloaded for each granule.
# These are the only variables needed by extract_points.py.
PIXC_VARS = [
    "longitude",
    "latitude",
    "height",
    "geoid",
    "classification",
    "water_frac",
    "water_frac_uncert",
    "cross_track",
    "interferogram_qual",
    "classification_qual",
    "geolocation_qual",
    "ancillary_surface_classification_flag",
    "sig0_qual",
]

DOWNLOAD_WORKERS = 12   # parallel OPeNDAP download threads

_print_lock = threading.Lock()


def tprint(*args, **kwargs):
    """Thread-safe print."""
    with _print_lock:
        print(*args, **kwargs, flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Plan and download SWOT PIXC granules.")
    parser.add_argument("--config",       required=True, help="Path to config.yaml")
    parser.add_argument("--region",       required=True, help="Region name (must match GRAIN parquet prefix)")
    parser.add_argument("--canal_start",  required=True, type=int, help="Start canal index (inclusive)")
    parser.add_argument("--canal_end",    required=True, type=int, help="End canal index (exclusive)")
    return parser.parse_args()


def scene_key(granule_name: str) -> str:
    """Strip the last two underscore-delimited tokens (version/counter) to get the scene key."""
    return "_".join(granule_name.split("_")[:-2])


def get_opendap_url(granule) -> str | None:
    """Return the OPeNDAP URL for a granule, or None if not available."""
    for u in granule.get("umm", {}).get("RelatedUrls", []):
        if (
            u.get("Type") == "USE SERVICE API"
            and "OPENDAP" in u.get("Subtype", "").upper()
        ):
            return u["URL"]
    return None


def download_opendap_subset(session, opendap_base: str, out_path: Path) -> None:
    """Download only the required PIXC_VARS from a granule via DAP4 subsetting."""
    query = ";".join([f"/pixel_cloud/{v}" for v in PIXC_VARS])
    url = f"{opendap_base}.dap.nc4?dap4.ce={query}"
    with session.get(url, stream=True) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)


def _download_one(granule_name, matched_granule, out_path, session):
    """Download one granule. Returns (granule_name, status, error_or_None)."""
    if out_path.exists():
        return granule_name, "skipped", None

    opendap_base = get_opendap_url(matched_granule)
    if opendap_base is not None:
        try:
            download_opendap_subset(session, opendap_base, out_path)
            return granule_name, "opendap", None
        except (requests.HTTPError, requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError) as e:
            if out_path.exists():
                out_path.unlink()
            return granule_name, "fallback", str(e)
    else:
        return granule_name, "fallback", None


def main():
    args = parse_args()

    config = yaml.safe_load(open(args.config))
    region = args.region

    run_base = Path(config["run_base_dir"])
    run_root = run_base / f"{region}_from_{args.canal_start}_to_{args.canal_end}"

    canals_path  = Path(config["base_grain_dir"]) / f"{region}_GRAIN_v.1.0.parquet"
    start_date   = config["start_date"]
    end_date     = config["end_date"]
    short_name   = config["pixc_short_name"]

    planning_dir = run_root / config["planning_dir"]
    download_dir = run_root / config["download_dir"]
    planning_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Load canal geometries for the requested index range
    canals = gpd.read_parquet(canals_path)
    canals = canals.sort_values("grain_id").iloc[args.canal_start:args.canal_end]
    print(f"[INFO] Processing canals {args.canal_start}:{args.canal_end}", flush=True)

    if canals.crs is None:
        raise RuntimeError("Canals CRS is missing — check the GRAIN parquet file.")

    canals = canals.to_crs(4326)
    min_lon, min_lat, max_lon, max_lat = canals.total_bounds
    region_bbox = (float(min_lon), float(min_lat), float(max_lon), float(max_lat))

    # EarthData login uses the ~/.netrc file or prompts interactively.
    # See README for setup instructions.
    auth    = earthaccess.login()
    session = auth.get_session()

    results = earthaccess.search_data(
        short_name=short_name,
        bounding_box=region_bbox,
        temporal=(start_date, end_date),
        cloud_hosted=False,
    )

    if not results:
        print("[INFO] No PIXC granules found for this region and time range.", flush=True)
        return

    print(f"[INFO] Total granules returned: {len(results)}", flush=True)

    # Build a spatial index of canal geometries in Web Mercator (EPSG:3857)
    canals_proj   = canals.to_crs(3857)
    canal_geoms   = list(canals_proj.geometry)
    canal_ids     = list(canals_proj["grain_id"])
    tree          = STRtree(canal_geoms)

    matched: Dict[str, dict] = {}
    pixc_to_grains: Dict[str, list] = {}

    for r in results:
        granule_name = r["meta"]["native-id"]
        geom_info    = (
            r.get("umm", {})
            .get("SpatialExtent", {})
            .get("HorizontalSpatialDomain", {})
            .get("Geometry", {})
        )

        granule_geom = None
        if "BoundingRectangles" in geom_info:
            bbox = geom_info["BoundingRectangles"][0]
            granule_geom = box(
                bbox["WestBoundingCoordinate"],
                bbox["SouthBoundingCoordinate"],
                bbox["EastBoundingCoordinate"],
                bbox["NorthBoundingCoordinate"],
            )
        elif "GPolygons" in geom_info:
            coords = geom_info["GPolygons"][0]["Boundary"]["Points"]
            lonlat = [(p["Longitude"], p["Latitude"]) for p in coords]
            granule_geom = Polygon(lonlat)

        if granule_geom is None:
            continue

        granule_geom_proj = (
            gpd.GeoSeries([granule_geom], crs=4326).to_crs(3857).iloc[0]
        )
        candidate_idxs = tree.query(granule_geom_proj)
        intersecting_grains = [
            str(canal_ids[idx])
            for idx in candidate_idxs
            if canal_geoms[idx].intersects(granule_geom_proj)
        ]
        if intersecting_grains:
            matched[granule_name] = r
            pixc_to_grains[granule_name] = intersecting_grains

    print(f"[INFO] Granule variants intersecting canals: {len(matched)}", flush=True)

    # Deduplicate scenes: prefer the variant that has an OPeNDAP URL
    scenes: Dict[str, str] = {}
    for granule_name, r in matched.items():
        key = scene_key(granule_name)
        if key not in scenes:
            scenes[key] = granule_name
        elif get_opendap_url(r) is not None and get_opendap_url(matched[scenes[key]]) is None:
            scenes[key] = granule_name

    selected_names = set(scenes.values())
    print(f"[INFO] Unique scenes after deduplication: {len(selected_names)}", flush=True)

    pixc_to_grains = {name: pixc_to_grains[name] for name in selected_names}

    out_json = planning_dir / "pixc_to_grains.json"
    with open(out_json, "w") as f:
        json.dump(pixc_to_grains, f, indent=2)

    out_unique = planning_dir / "unique_pixc_granules.txt"
    out_unique.write_text("\n".join(sorted(pixc_to_grains.keys())) + "\n")

    # ── Parallel OPeNDAP downloads ────────────────────────────────────────────
    total = len(pixc_to_grains)
    print(f"[INFO] Downloading {total} granules with {DOWNLOAD_WORKERS} parallel threads...", flush=True)

    to_fallback = []
    n_opendap   = 0
    n_skipped   = 0
    n_done      = 0

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
        futures = {
            executor.submit(
                _download_one,
                name,
                matched[name],
                download_dir / f"{name}.nc",
                session,
            ): name
            for name in pixc_to_grains
        }
        for future in as_completed(futures):
            granule_name, status, err = future.result()
            n_done += 1
            if status == "skipped":
                n_skipped += 1
            elif status == "opendap":
                n_opendap += 1
            elif status == "fallback":
                if err:
                    tprint(f"[WARN] OPeNDAP failed for {granule_name}: {err} — queuing fallback")
                to_fallback.append(matched[granule_name])
            if n_done % 50 == 0 or n_done == total:
                tprint(
                    f"[INFO] Progress: {n_done}/{total}  "
                    f"(opendap={n_opendap}, skipped={n_skipped}, fallback_queued={len(to_fallback)})"
                )

    if to_fallback:
        print(f"[INFO] Full download for {len(to_fallback)} granule(s) without OPeNDAP...", flush=True)
        earthaccess.login()   # re-authenticate in case session expired
        earthaccess.download(to_fallback, str(download_dir))

    print(
        f"[OK] Planning and download complete. "
        f"OPeNDAP: {n_opendap}, full: {len(to_fallback)}, already present: {n_skipped}",
        flush=True,
    )


if __name__ == "__main__":
    main()
