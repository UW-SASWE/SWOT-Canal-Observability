# SWOT-Canal-Observability

This pipeline processes NASA SWOT satellite pixel-cloud (PIXC) data to compute
water surface elevation (WSE) slopes and observability confidence levels for
irrigation canal segments in the GRAIN dataset.

**What it produces:** For each canal segment in a region, the pipeline outputs
a *confidence level* (CL_A ∈ [0, 1]) that quantifies how reliably the SWOT
satellite can observe water surface elevation over that canal.

---

## Table of Contents

1. [What you need before starting](#1-what-you-need-before-starting)
2. [Installation](#2-installation)
3. [Download the GRAIN canal dataset](#3-download-the-grain-canal-dataset)
4. [Set up NASA EarthData access](#4-set-up-nasa-earthdata-access)
5. [Set up Google Earth Engine and Cloud Storage](#5-set-up-google-earth-engine-and-cloud-storage)
6. [Configure the pipeline](#6-configure-the-pipeline)
7. [Run the pipeline](#7-run-the-pipeline)
8. [Compute confidence levels (notebook)](#8-compute-confidence-levels-notebook)
9. [Output files explained](#9-output-files-explained)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What you need before starting

### Accounts and services required

| Service | What it is used for | Free tier? |
|---------|---------------------|-----------|
| [NASA EarthData](https://urs.earthaccess.nasa.gov/users/new) | Downloading SWOT PIXC satellite data | Yes |
| [Google Earth Engine](https://earthengine.google.com/signup/) | Sampling a 30 m terrain elevation model (DEM) along canal centrelines | Free for research |
| [Google Cloud Storage](https://cloud.google.com/storage) | Temporary storage for GEE export files | Small cost (a few cents per region) |

### Software requirements

- Linux or macOS (Windows via WSL2 should also work)
- [conda](https://docs.conda.io/en/latest/miniconda.html) or [mamba](https://mamba.readthedocs.io/) package manager
- Python 3.10 or later (installed via conda below)
- ~50 GB of free disk space per region (PIXC files are large but deleted after extraction)

---

## 2. Installation

### 2.1 Clone this repository

```bash
git clone https://github.com/YOUR_USERNAME/SOGRAIN2.0-pipeline.git
cd SOGRAIN2.0-pipeline
```

### 2.2 Create the conda environment

```bash
conda env create -f environment.yaml
conda activate swot-grain   # environment named swot-grain
```

This installs all required Python packages. The environment is named `swot-grain`.

### 2.3 Verify the installation

```bash
snakemake --version      # should print 7.x or 8.x
python -c "import earthaccess; print('earthaccess OK')"
python -c "import ee; print('earthengine OK')"
```

---

## 3. Download the GRAIN canal dataset

The pipeline uses the **GRAIN v1.0** dataset of global irrigation canal
centrelines.

**Download link:** https://doi.org/10.5281/zenodo.16786487

1. Go to the link above and download the GeoParquet archive.
2. Extract it. You should have a directory containing files like:
   `iran_GRAIN_v.1.0.parquet`, `thailand_GRAIN_v.1.0.parquet`, etc.
3. Note the full path to this directory — you will need it in Step 6.

> **Tip:** Each `.parquet` file corresponds to one region. The file name prefix
> (e.g. `iran`) is the `region_name` you will use in `config.yaml`.

---

## 4. Set up NASA EarthData access

SWOT PIXC data is downloaded automatically by the pipeline using the
`earthaccess` library, which reads your EarthData credentials.

### 4.1 Create an EarthData account

If you do not already have one, register at:
https://urs.earthaccess.nasa.gov/users/new

### 4.2 Store your credentials

Run the following once in your terminal (with the `swot-grain` environment active):

```bash
python -c "import earthaccess; earthaccess.login(strategy='interactive', persist=True)"
```

This saves your username and password to `~/.netrc` so you do not have to log
in manually each time.

---

## 5. Set up Google Earth Engine and Cloud Storage

The DEM sampling step (Step 4 of the pipeline) uses Google Earth Engine (GEE)
to sample the Copernicus GLO-30 terrain model along canal centrelines, and
Google Cloud Storage (GCS) as a staging area for the results.

### 5.1 Authenticate with Google Earth Engine

```bash
earthengine authenticate
```

Follow the browser prompt. This stores credentials in `~/.config/earthengine/`.

### 5.2 Create a Google Cloud project and bucket

1. Go to https://console.cloud.google.com and create a project (or use an existing one).
2. Enable the **Earth Engine API** and **Cloud Storage API** for your project.
3. Create a GCS bucket (e.g. `my-sograin-dem-exports`).
4. Authenticate the `gcloud` SDK for application-default credentials:

```bash
gcloud auth application-default login
```

### 5.3 Upload GRAIN canal features to Earth Engine

The `dem_sample.py` script requires your GRAIN canals as a GEE FeatureCollection asset.

1. Convert the GRAIN parquet file for your region to GeoJSON or Shapefile
   (geopandas can do this):

```python
import geopandas as gpd
canals = gpd.read_parquet("iran_GRAIN_v.1.0.parquet")
canals.to_file("iran_GRAIN_canals.geojson", driver="GeoJSON")
```

2. Upload to GEE via the [Code Editor](https://code.earthengine.google.com/)
   (Assets → New → Table upload) or via the command line:

```bash
earthengine upload table \
    --asset_id projects/YOUR_GEE_PROJECT/assets/GRAIN/iran_GRAIN_canals \
    iran_GRAIN_canals.geojson
```

The asset path `projects/YOUR_GEE_PROJECT/assets/GRAIN` is what you set as
`base_gee_asset` in `config.yaml`.

---

## 6. Configure the pipeline

Open `config.yaml` in a text editor and fill in the **USER-SUPPLIED** fields:

```yaml
# Path to the directory containing the GRAIN parquet files from Step 3
base_grain_dir: /path/to/GRAIN_v.1.0/GeoParquet

# Directory where all pipeline outputs will be written
run_base_dir: /path/to/your/output/directory

# GEE asset base path (from Step 5.3)
base_gee_asset: projects/YOUR_GEE_PROJECT/assets/GRAIN

# GCS bucket name (from Step 5.2)
gcs_bucket: my-sograin-dem-exports

# GCP project ID
gcp_project: my-gcp-project-id

# Region to process (must match the GRAIN parquet file prefix)
region_name: iran

# Canal index range (0-based).
# To process the entire region use canal_start=0 and
# canal_end = total number of rows in the parquet file.
# For a quick test, start with a small range like 0–100.
canal_start: 0
canal_end: 100
```

> **Finding the total number of canals in a region:**
> ```python
> import geopandas as gpd
> df = gpd.read_parquet("/path/to/GRAIN_v.1.0/GeoParquet/iran_GRAIN_v.1.0.parquet")
> print(len(df))   # use this number as canal_end
> ```

### Optional filtering parameters

By default the pipeline keeps only PIXC pixels classified as **open water
(class 4)**. You can extend this or add a cross-track distance filter:

```yaml
# Include partial surface water pixels as well
classification: [3, 4]

# Restrict to pixels between 10–60 km from the satellite nadir track
use_cross_track_filter: true
cross_track_min_m: 10000
cross_track_max_m: 60000
```

---

## 7. Run the pipeline

Make sure the `swot-grain` environment is active, then from the repository root:

```bash
conda activate swot-grain
snakemake --cores 8
```

Replace `8` with the number of CPU cores available on your machine. The
pipeline parallelises the extraction step across canal chunks automatically.

### What happens step by step

| Step | Script | What it does |
|------|--------|--------------|
| 1 | `plan_and_download_pixc.py` | Searches NASA EarthData for SWOT PIXC granules that overlap your canals and downloads them via OPeNDAP (only the variables needed are fetched — much faster than full-file download). |
| 2 | `extract_points.py` (×N chunks) | For each downloaded granule, extracts pixel-cloud points within `buffer_m` metres of each canal centreline and writes them to Parquet files. |
| 3 | `compute_wse_metrics.py` | Bins the extracted points along each canal, computes median WSE per bin, and fits a Theil-Sen robust slope. |
| 4 | `dem_sample.py` | Samples the Copernicus GLO-30 DEM along each canal via GEE, fits a terrain slope, and compares it with the SWOT slope. |

### Outputs are written to

```
{run_base_dir}/{region_name}_from_{canal_start}_to_{canal_end}/
├── pixc_planning/
│   ├── pixc_to_grains.json          # which granules cover which canals
│   └── unique_pixc_granules.txt
├── pixc_downloads/                  # PIXC .nc files (deleted after extraction)
├── extracted_points/                # per-canal Parquet files
├── metrics/
│   ├── wse_metrics.csv              # WSE slope metrics per canal
│   ├── dem_samples.csv              # raw DEM elevation samples
│   └── wse_dem_slope_comparison.csv # final table: WSE + DEM slopes merged
└── run_metadata/
    └── run_config.yaml              # config snapshot + timestamp + git commit
```

### Testing with a small subset

Before processing an entire region (which can take several hours), it is
recommended to test with a small range of canals, e.g.:

```bash
snakemake --cores 4 --config canal_start=0 canal_end=50
```

---

## 8. Compute confidence levels (notebook)

After the pipeline finishes, open the Jupyter notebook to compute CL_A:

```bash
jupyter notebook notebooks/compute_confidence_CL_A.ipynb
```

1. In **Cell 1**, set `PIPELINE_OUTPUT_CSV` to the path of
   `wse_dem_slope_comparison.csv` produced by the pipeline.
2. Run all cells (Kernel → Restart & Run All).
3. The notebook will:
   - Compute individual metric scores (SNR, residual noise, coverage, contiguity, slope direction, slope magnitude agreement).
   - Combine them into three components: physical realism (`s_physical_realism`), statistical robustness (`s_statistical_robustness`), and spatial coherence (`s_spatial_coherence`).
   - Compute CL_A using the geometric-mean + minimum-dimension penalty formula.
   - Show quantile plots and class distribution charts.
   - Save `confidence_levels_CL_A.csv` alongside the input file.

### Understanding CL_A

| CL_A range | Class | Interpretation |
|-----------|-------|---------------|
| 0.00 – 0.20 | Low | SWOT rarely produces usable observations for this canal |
| 0.20 – 0.66 | Moderate | Observations available but with caveats |
| 0.66 – 1.00 | High | SWOT reliably observes this canal |

Canals without a DEM slope (e.g. very short segments that GEE could not sample)
will have `CL_A = NaN` in the output.

---

## 9. Output files explained

### `wse_metrics.csv`

| Column | Units | Description |
|--------|-------|-------------|
| `grain_id` | — | GRAIN canal segment identifier (matches `grain_id` in the GRAIN dataset) |
| `n_bins` | — | Number of along-canal bins with at least one PIXC pixel |
| `wse_slope_m_per_m` | m/m | Theil-Sen WSE slope (positive = elevation increases with distance) |
| `wse_slope_abs_m_per_m` | m/m | Absolute value of WSE slope |
| `wse_noise_sigma_m` | m | MAD-based robust residual standard deviation (noise level) |
| `canal_length_m` | m | Observed canal length (span of occupied bins) |
| `obs_coverage_frac` | — | Fraction of canal span covered by observations (0–1) |
| `obs_contiguity_frac` | — | Longest unbroken run of observed bins as fraction of total span (0–1) |
| `wse_slope_snr` | — | Signal-to-noise ratio: `wse_slope_abs × canal_length / wse_noise_sigma` |

### `wse_dem_slope_comparison.csv`

All columns from `wse_metrics.csv` plus:

| Column | Description |
|--------|-------------|
| `dem_slope_m_per_m` | Theil-Sen DEM terrain slope along the canal |
| `dem_slope_sign` | Sign of DEM slope (−1, 0, or +1) |
| `wse_slope_sign` | Sign of WSE slope |
| `slope_sign_match` | True if WSE and DEM slopes have the same sign |
| `slope_sign_category` | "match" or "mismatch" |

### `confidence_levels_CL_A.csv` (from notebook)

All of the above plus:

| Column | Description |
|--------|-------------|
| `slope_magnitude_diff_m_per_m` | Absolute difference between WSE and DEM slope magnitudes |
| `score_slope_direction` | Direction-agreement score (0–1) |
| `score_slope_magnitude` | Slope-magnitude-consistency score (0–1) |
| `score_snr` | SNR score (0–1) |
| `score_dispersion` | Noise/dispersion score (0–1, higher = lower noise) |
| `score_coverage` | Spatial coverage score (0–1) |
| `score_contiguity` | Spatial contiguity score (0–1) |
| `s_physical_realism` | Physical realism component score (0–1) |
| `s_statistical_robustness` | Statistical robustness component score (0–1) |
| `s_spatial_coherence` | Spatial coherence component score (0–1) |
| `CL_A` | Final observability confidence level (0–1) |

---

## 10. Troubleshooting

**`No PIXC granules found`**
Check that your `region_name` matches an actual GRAIN parquet file with
canal geometries that fall within the SWOT swath. SWOT has a ~21-day repeat
cycle; some small or inland regions may have sparse coverage.

**`Canals CRS missing`**
The GRAIN parquet file must include a CRS. Re-download from Zenodo if this
message appears.

**`GEE task failed`**
GEE export errors are usually caused by hitting the 10 MB table size limit.
Try reducing `dem_batch_size` in `config.yaml` (e.g. to 200).

**`GCS object not found`**
Verify that `gcs_bucket` and `gcp_project` in `config.yaml` are correct and
that your service account has read/write access to the bucket.

**OPeNDAP download failures**
The pipeline automatically falls back to full-file download. If you see many
fallback warnings, check your `~/.netrc` credentials.

**Memory errors during extraction**
Reduce `chunk_size` in `config.yaml` (e.g. from 300 to 50). Each chunk
processes that many canals in parallel in a single process.

---

## Citation

If you use this pipeline or the resulting confidence levels in published work,
please cite the GRAIN dataset:

> [GRAIN v1.0 — Global River and Irrigation Network (Zenodo)](https://doi.org/10.5281/zenodo.16786487)

---

## License

MIT License. See `LICENSE` for details.
