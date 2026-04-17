"""
compute_wse_metrics.py

Step 3 of the SOGRAIN 2.0 pipeline.

Reads the per-canal Parquet files produced by extract_points.py and computes
water surface elevation (WSE) slope metrics for each canal segment.

Method:
  - Filter pixels to the allowed PIXC classification values (default: [4] = open water).
  - Optionally apply a cross-track distance range filter (disabled by default).
  - Compute WSE = height - geoid.
  - Bin pixels along the canal by distance from the upstream end.
  - Take the median WSE in each bin.
  - Fit a Theil-Sen robust regression to get slope and intercept.
  - Derive residual sigma, spatial coverage, contiguity, and slope SNR.

Config options that control this step:
  classification          list of allowed PIXC classes  (default: [4])
  use_cross_track_filter  enable cross-track range filter  (default: false)
  cross_track_min_m       min |cross_track| in metres  (default: 10000)
  cross_track_max_m       max |cross_track| in metres  (default: 60000)
  bin_width_m             bin width along the canal  (default: 50)
  min_bins_required       minimum occupied bins to fit a slope  (default: 5)

Output:  {run_base_dir}/{region}_from_{start}_to_{end}/metrics/wse_metrics.csv
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import theilslopes
import yaml


def robust_sigma(residuals):
    """MAD-based robust standard deviation (consistent with Gaussian sigma)."""
    med = np.median(residuals)
    mad = np.median(np.abs(residuals - med))
    return 1.4826 * mad


def compute_metrics(df, config):
    """
    Compute WSE slope metrics for one canal segment.

    Filtering:
      - Keeps only rows whose PIXC classification is in config['classification'].
        Default [4] = open water only.
      - If config['use_cross_track_filter'] is True, also restricts pixels to
        the cross-track distance range [cross_track_min_m, cross_track_max_m].

    Returns a dict of metrics, or None if there are too few valid bins.
    """
    BIN_WIDTH = config.get("bin_width_m", 50)
    MIN_BINS  = config.get("min_bins_required", 5)

    CLASS_ALLOWED = set(config.get("classification", [4]))
    df = df[df["classification"].isin(CLASS_ALLOWED)].copy()
    if df.empty:
        return None

    # Optional cross-track distance filter (reduces layover artefacts near swath edges)
    if config.get("use_cross_track_filter", False):
        CT_MIN = float(config["cross_track_min_m"])
        CT_MAX = float(config["cross_track_max_m"])
        df = df[
            (df["cross_track"].abs() >= CT_MIN) &
            (df["cross_track"].abs() <= CT_MAX)
        ]
        if df.empty:
            return None

    df["wse"] = df["height"] - df["geoid"]
    df = df[np.isfinite(df["wse"])]
    if df.empty:
        return None

    # Bin pixels along the canal centreline
    df["bin"] = (df["d_m"] // BIN_WIDTH).astype(int)
    grouped   = df.groupby("bin")

    bin_centers, medians = [], []
    for b, g in grouped:
        bin_centers.append((b + 0.5) * BIN_WIDTH)
        medians.append(np.median(g["wse"]))

    if len(bin_centers) < MIN_BINS:
        return None

    d_vals   = np.array(bin_centers)
    wse_vals = np.array(medians)

    slope, intercept, _, _ = theilslopes(wse_vals, d_vals)

    residuals    = wse_vals - (intercept + slope * d_vals)
    sigma        = robust_sigma(residuals)
    canal_length = d_vals.max() - d_vals.min()

    # Coverage: fraction of the canal span occupied by at least one bin
    bins_sorted        = np.sort(df["bin"].unique())
    min_bin, max_bin   = bins_sorted.min(), bins_sorted.max()
    total_possible     = max_bin - min_bin + 1
    coverage_frac      = len(bins_sorted) / total_possible

    # Contiguity: longest unbroken run of occupied bins / total possible bins
    max_run = current_run = 1
    for i in range(1, len(bins_sorted)):
        if bins_sorted[i] == bins_sorted[i - 1] + 1:
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 1
    contig_frac = max_run / total_possible

    abs_slope   = abs(slope)
    signal_amp  = abs_slope * canal_length
    slope_snr   = signal_amp / sigma if sigma > 0 else np.nan

    return {
        "n_bins":                    len(d_vals),
        "wse_slope_m_per_m":         slope,
        "wse_slope_abs_m_per_m":     abs_slope,
        "wse_noise_sigma_m":         sigma,
        "canal_length_m":            canal_length,
        "obs_coverage_frac":         coverage_frac,
        "obs_contiguity_frac":       contig_frac,
        "wse_slope_snr":             slope_snr,
    }


def process_one(parquet_file, config):
    """Worker: read one canal Parquet file and return its metric record."""
    grain_id = parquet_file.stem
    try:
        df = pd.read_parquet(parquet_file)
    except Exception as e:
        return None, f"[WARNING] Failed to read {parquet_file}: {e}"

    result = compute_metrics(df, config)
    if result is not None:
        result["grain_id"] = grain_id

    return result, None


def parse_args():
    parser = argparse.ArgumentParser(description="Compute WSE slope metrics per canal.")
    parser.add_argument("--config",       required=True)
    parser.add_argument("--region",       required=True)
    parser.add_argument("--canal_start",  required=True, type=int)
    parser.add_argument("--canal_end",    required=True, type=int)
    parser.add_argument("--chunk_size",   type=int, default=None,
                        help="Chunk size used during extraction (default: read from config)")
    parser.add_argument("--cores",        type=int, default=1,
                        help="Number of parallel worker processes (default: 1)")
    return parser.parse_args()


def main():
    args   = parse_args()
    config = yaml.safe_load(open(args.config))

    chunk_size = args.chunk_size or int(config.get("chunk_size", 100))

    run_base = Path(config["run_base_dir"])
    run_root = run_base / f"{args.region}_from_{args.canal_start}_to_{args.canal_end}"

    extracted_base = run_root / config["extracted_points_dir"]
    out_dir        = run_root / config["metrics_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    # Derive chunk boundaries (must mirror the Snakefile logic)
    chunks = [
        (i, min(i + chunk_size, args.canal_end))
        for i in range(args.canal_start, args.canal_end, chunk_size)
    ]

    all_parquet_files = []
    missing_chunks    = []

    for start, end in chunks:
        chunk_dir = extracted_base / f"chunk_{start}_{end}"
        if not chunk_dir.exists():
            missing_chunks.append(chunk_dir)
            continue
        all_parquet_files.extend(sorted(chunk_dir.glob("*.parquet")))

    if missing_chunks:
        print(f"[WARNING] {len(missing_chunks)} chunk director(ies) not found (extraction may be incomplete):")
        for d in missing_chunks:
            print(f"    {d}")

    if not all_parquet_files:
        raise RuntimeError(f"No parquet files found under {extracted_base}.")

    print(f"[INFO] {len(chunks)} chunks, {len(all_parquet_files)} parquet files, cores={args.cores}.")

    records   = []
    total     = len(all_parquet_files)
    log_every = max(1, total // 20)   # print roughly every 5%

    if args.cores == 1:
        for i, parquet_file in enumerate(all_parquet_files, 1):
            result, warn = process_one(parquet_file, config)
            if warn:
                print(warn)
            if result is not None:
                records.append(result)
            if i % log_every == 0 or i == total:
                print(f"[PROGRESS] {i}/{total} ({100*i//total}%)  valid={len(records)}")
    else:
        with ProcessPoolExecutor(max_workers=args.cores) as exe:
            futures = {exe.submit(process_one, f, config): f for f in all_parquet_files}
            done    = 0
            for future in as_completed(futures):
                result, warn = future.result()
                if warn:
                    print(warn)
                if result is not None:
                    records.append(result)
                done += 1
                if done % log_every == 0 or done == total:
                    print(f"[PROGRESS] {done}/{total} ({100*done//total}%)  valid={len(records)}", flush=True)

    out_csv = out_dir / "wse_metrics.csv"
    pd.DataFrame(records).to_csv(out_csv, index=False)
    print(f"[OK] {len(records)} canals written to {out_csv}")


if __name__ == "__main__":
    main()
