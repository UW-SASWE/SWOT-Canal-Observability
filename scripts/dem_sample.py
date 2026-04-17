"""
dem_sample.py

Step 4 of the SOGRAIN 2.0 pipeline.

Samples the Copernicus GLO-30 DEM along each canal centreline using Google
Earth Engine (GEE), computes a Theil-Sen slope for each canal from the DEM
samples, and merges the result with the WSE slope metrics to produce a final
comparison table that includes slope sign agreement.

Prerequisites:
  - A GEE account with the GRAIN canal features uploaded as a FeatureCollection.
    See README for upload instructions.
  - A Google Cloud Storage bucket for intermediate GEE exports.
  - `earthengine authenticate` and `gcloud auth application-default login`
    run at least once before executing this script.

Output: {run_base_dir}/{region}_from_{start}_to_{end}/metrics/wse_dem_slope_comparison.csv
"""

import argparse
from datetime import datetime, UTC
import time
from pathlib import Path
import math
import numpy as np
from scipy.stats import theilslopes
import pandas as pd
import ee
from google.cloud import storage
import yaml


def init_ee():
    """Initialise Earth Engine, triggering authentication if needed."""
    try:
        ee.Initialize()
    except Exception:
        ee.Authenticate()
        ee.Initialize()


def wait_for_task(task):
    """Poll a GEE export task until it completes or fails."""
    print("[INFO] Waiting for GEE export...")
    while task.active():
        print("   State:", task.status()["state"])
        time.sleep(30)

    status = task.status()
    if status["state"] != "COMPLETED":
        raise RuntimeError(f"GEE task failed: {status}")

    print("[OK] Export completed")


def download_from_gcs(bucket_name, blob_name, out_path, project):
    """Download a single file from Google Cloud Storage."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    client = storage.Client(project=project)
    bucket = client.bucket(bucket_name)
    blob   = bucket.blob(blob_name)

    if not blob.exists():
        raise RuntimeError(f"GCS object not found: gs://{bucket_name}/{blob_name}")

    blob.download_to_filename(out_path)
    print(f"[OK] Downloaded {out_path}")


def chunk_list(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def parse_args():
    parser = argparse.ArgumentParser(description="Sample DEM along canals and compare with WSE slopes.")
    parser.add_argument("--config",       required=True, help="Path to config.yaml")
    parser.add_argument("--region",       required=True, help="Region name")
    parser.add_argument("--canal_start",  required=True, type=int)
    parser.add_argument("--canal_end",    required=True, type=int)
    return parser.parse_args()


def main():
    args = parse_args()

    config = yaml.safe_load(open(args.config))
    region = args.region

    run_base = Path(config["run_base_dir"])
    run_root = run_base / f"{region}_from_{args.canal_start}_to_{args.canal_end}"

    # GEE asset path: base_gee_asset/{region}_GRAIN_canals
    gee_asset = f"{config['base_gee_asset']}/{region}_GRAIN_canals"

    metrics_csv = run_root / config["metrics_dir"] / "wse_metrics.csv"
    if not metrics_csv.exists():
        raise RuntimeError(f"Missing wse_metrics.csv at {metrics_csv}")

    df         = pd.read_csv(metrics_csv)
    canal_ids  = df["grain_id"].dropna().unique().tolist()
    if not canal_ids:
        print("[SKIP] No canals found in wse_metrics.csv — nothing to do.")
        return

    print(f"[INFO] Sampling DEM along {len(canal_ids)} canals")

    init_ee()

    canals     = ee.FeatureCollection(gee_asset)
    SPACING_M  = config["dem_spacing_m"]
    DEM_SCALE  = config["dem_scale_m"]
    BATCH_SIZE = int(config.get("dem_batch_size", 1000))

    def points_along(feature):
        """Generate evenly spaced sample points along a canal centreline in GEE."""
        geom   = ee.Geometry(feature.geometry())
        length = geom.length()
        dists  = ee.List.sequence(0, length, SPACING_M)
        cut    = geom.cutLines(dists)
        segs   = ee.List(cut.coordinates())

        pts = segs.map(
            lambda seg: ee.Feature(
                ee.Geometry.Point(ee.List(seg).get(0)),
                {"grain_id": feature.get("grain_id")}
            )
        )
        pts = ee.List(pts).zip(dists).map(
            lambda z: ee.Feature(ee.List(z).get(0)).set("dist_m", ee.List(z).get(1))
        )
        return ee.FeatureCollection(pts)

    dem = ee.ImageCollection("COPERNICUS/DEM/GLO30").mosaic().select("DEM")

    batch_dir        = run_root / config["metrics_dir"] / "dem_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    local_batch_csvs = []

    id_batches = list(chunk_list(canal_ids, BATCH_SIZE))
    print(f"[INFO] Total DEM batches: {len(id_batches)}  (batch size = {BATCH_SIZE})")

    for batch_idx, batch_ids in enumerate(id_batches, start=1):
        print(f"[INFO] Processing DEM batch {batch_idx}/{len(id_batches)} ({len(batch_ids)} canals)")

        target_canals = canals.filter(ee.Filter.inList("grain_id", batch_ids))
        gee_count     = target_canals.size().getInfo()
        print(f"[INFO] GEE canal count in batch: {gee_count}")

        if gee_count == 0:
            print("[WARN] No matching canals in GEE asset for this batch — skipping.")
            continue

        sample_points = target_canals.map(points_along).flatten()
        dem_samples   = dem.sampleRegions(
            collection=sample_points,
            scale=DEM_SCALE,
            geometries=False,
        )

        timestamp   = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        export_name = f"{region}_dem_samples_b{batch_idx:03d}_{timestamp}"

        task = ee.batch.Export.table.toCloudStorage(
            collection=dem_samples,
            description=export_name,
            bucket=config["gcs_bucket"],
            fileNamePrefix=export_name,
            fileFormat="CSV",
        )
        task.start()
        print(f"[INFO] GEE export started for batch {batch_idx}")

        wait_for_task(task)

        local_out = batch_dir / f"{export_name}.csv"
        download_from_gcs(
            bucket_name=config["gcs_bucket"],
            blob_name=f"{export_name}.csv",
            out_path=local_out,
            project=config["gcp_project"],
        )
        local_batch_csvs.append(local_out)

    if not local_batch_csvs:
        raise RuntimeError("No DEM batch CSVs were produced — check GEE asset and bucket permissions.")

    dem_frames = [pd.read_csv(p) for p in local_batch_csvs if not pd.read_csv(p).empty]
    if not dem_frames:
        print("[WARN] All DEM batch files are empty.")
        return

    dem_df    = pd.concat(dem_frames, ignore_index=True)
    local_out = run_root / config["metrics_dir"] / "dem_samples.csv"
    dem_df.to_csv(local_out, index=False)
    print(f"[OK] Combined DEM samples written to {local_out}")

    TOL = float(config.get("slope_sign_tolerance", 1e-6))

    def slope_sign(x):
        if pd.isna(x):
            return np.nan
        x = float(x)
        if abs(x) < TOL:
            return 0
        return np.sign(x)

    # Fit Theil-Sen slope to DEM elevation vs. along-canal distance
    slope_results = []
    for grain_id, g in dem_df.groupby("grain_id"):
        g = g.sort_values("dist_m")

        d_vals    = pd.to_numeric(g["dist_m"], errors="coerce").values
        elev_vals = pd.to_numeric(g["DEM"],    errors="coerce").values

        valid     = np.isfinite(d_vals) & np.isfinite(elev_vals)
        d_vals    = d_vals[valid]
        elev_vals = elev_vals[valid]

        if len(d_vals) < 8:
            continue

        slope_val, *_ = theilslopes(elev_vals, d_vals)
        slope_results.append({
            "grain_id":    grain_id,
            "dem_slope_m_per_m": slope_val,
            "dem_slope_sign":    slope_sign(slope_val),
        })

    dem_slope_df = pd.DataFrame(slope_results)

    # Merge DEM slopes with WSE metrics and compute slope-sign agreement
    wse_df = pd.read_csv(metrics_csv)
    wse_df["grain_id"]       = wse_df["grain_id"].astype(str)
    dem_slope_df["grain_id"] = dem_slope_df["grain_id"].astype(str)

    merged = wse_df.merge(dem_slope_df, on="grain_id", how="left")
    merged["wse_slope_sign"]      = merged["wse_slope_m_per_m"].apply(slope_sign)
    merged["slope_sign_match"]    = merged["wse_slope_sign"] == merged["dem_slope_sign"]
    merged["slope_sign_category"] = np.where(merged["slope_sign_match"], "match", "mismatch")

    out_path = run_root / config["metrics_dir"] / "wse_dem_slope_comparison.csv"
    merged.to_csv(out_path, index=False)
    print(f"[OK] Written: {out_path}")
    print("[OK] DEM slope comparison complete.")


if __name__ == "__main__":
    main()
