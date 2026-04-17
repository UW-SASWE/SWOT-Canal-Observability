# ============================================================
# SOGRAIN 2.0 — Fast Pipeline Snakefile
# ============================================================
# Usage:
#   snakemake --cores 8
#
# Override config values on the command line, e.g.:
#   snakemake --cores 8 --config region_name=thailand canal_start=0 canal_end=500
# ============================================================

from pathlib import Path

configfile: "config.yaml"

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

REGION      = config["region_name"]
CANAL_START = int(config["canal_start"])
CANAL_END   = int(config["canal_end"])

# Which config file the Python scripts should read.
CONFIG_FILE = config.get("config_file", "config.yaml")

RUN_BASE = config["run_base_dir"]
RUN_ROOT = Path(RUN_BASE) / f"{REGION}_from_{CANAL_START}_to_{CANAL_END}"

CHUNK_SIZE    = int(config.get("chunk_size", 300))
COMPUTE_CORES = int(config.get("compute_cores", 4))

# ------------------------------------------------------------
# HELPER
# ------------------------------------------------------------

def r(path):
    """Resolve a path relative to the run root directory."""
    return RUN_ROOT / path

# ------------------------------------------------------------
# CHUNK BOUNDARIES  (mirrored in compute_wse_metrics.py)
# ------------------------------------------------------------

CHUNKS = [
    (i, min(i + CHUNK_SIZE, CANAL_END))
    for i in range(CANAL_START, CANAL_END, CHUNK_SIZE)
]

CHUNK_STARTS = [c[0] for c in CHUNKS]
CHUNK_ENDS   = [c[1] for c in CHUNKS]

# ============================================================
# FINAL TARGET
# ============================================================

rule all:
    input:
        r("metrics/wse_dem_slope_comparison.csv"),
        r("run_metadata/run_config.yaml")

# ============================================================
# SAVE RUN METADATA
# ============================================================

rule save_run_metadata:
    output:
        r("run_metadata/run_config.yaml")
    run:
        import yaml
        from datetime import datetime
        import subprocess
        Path(output[0]).parent.mkdir(parents=True, exist_ok=True)

        meta = dict(config)
        meta["timestamp"] = datetime.now().isoformat()

        try:
            meta["git_commit"] = subprocess.check_output(
                ["git", "rev-parse", "HEAD"]
            ).decode().strip()
        except Exception:
            meta["git_commit"] = "no_git_repo"

        with open(output[0], "w") as f:
            yaml.dump(meta, f)

# ============================================================
# PLAN + DOWNLOAD PIXC (PARALLEL OPENDAP)
# ============================================================

rule plan:
    output:
        r("pixc_planning/pixc_to_grains.json")
    shell:
        """
        python scripts/plan_and_download_pixc.py \
            --config {CONFIG_FILE} \
            --region {REGION} \
            --canal_start {CANAL_START} \
            --canal_end {CANAL_END}
        """

# ============================================================
# CHUNKED EXTRACTION (PARALLEL)
# ============================================================

rule extract_chunk:
    input:
        r("pixc_planning/pixc_to_grains.json")
    output:
        r("extracted_points/chunk_{start}_{end}/extraction_complete.flag")
    params:
        chunk_id=lambda wildcards: f"{wildcards.start}_{wildcards.end}"
    threads: 1
    resources:
        mem_mb=2300
    shell:
        """
        python scripts/extract_points.py \
            --config {CONFIG_FILE} \
            --region {REGION} \
            --run_start {CANAL_START} \
            --run_end {CANAL_END} \
            --canal_start {wildcards.start} \
            --canal_end {wildcards.end} \
            --chunk_id {params.chunk_id}
        """

# ------------------------------------------------------------
# Barrier: wait for all extraction chunks
# ------------------------------------------------------------

rule wait_for_all_chunks:
    input:
        expand(
            r("extracted_points/chunk_{start}_{end}/extraction_complete.flag"),
            zip,
            start=CHUNK_STARTS,
            end=CHUNK_ENDS
        )
    output:
        r("extracted_points/all_chunks_complete.flag")
    shell:
        "touch {output}"

# ============================================================
# MERGE CHUNKS (flag only — parquet files stay per-chunk)
# ============================================================

rule merge_chunks:
    input:
        r("extracted_points/all_chunks_complete.flag")
    output:
        r("extracted_points/extraction_complete.flag")
    shell:
        "touch {output}"

# ============================================================
# CLEANUP DOWNLOADED PIXC FILES
# ============================================================

rule cleanup_pixc:
    input:
        r("extracted_points/extraction_complete.flag")
    output:
        r("pixc_downloads/cleanup_complete.flag")
    params:
        download_dir=r("pixc_downloads")
    shell:
        """
        rm -f {params.download_dir}/*.nc
        touch {output}
        """

# ============================================================
# COMPUTE WSE METRICS (PARALLEL ACROSS CANALS)
# ============================================================

rule compute:
    input:
        r("pixc_downloads/cleanup_complete.flag")
    output:
        r("metrics/wse_metrics.csv")
    threads: COMPUTE_CORES
    shell:
        """
        python scripts/compute_wse_metrics.py \
            --config {CONFIG_FILE} \
            --region {REGION} \
            --canal_start {CANAL_START} \
            --canal_end {CANAL_END} \
            --chunk_size {CHUNK_SIZE} \
            --cores {threads}
        """

# ============================================================
# DEM SAMPLING VIA GOOGLE EARTH ENGINE
# ============================================================

rule dem:
    input:
        r("metrics/wse_metrics.csv")
    output:
        r("metrics/wse_dem_slope_comparison.csv")
    shell:
        """
        python scripts/dem_sample.py \
            --config {CONFIG_FILE} \
            --region {REGION} \
            --canal_start {CANAL_START} \
            --canal_end {CANAL_END}
        """
