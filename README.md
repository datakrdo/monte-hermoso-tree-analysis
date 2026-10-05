🇬🇧 [English](README.md) · 🇪🇸 [Español](README_es.md)

# Monte Hermoso Tree Analysis 🌳🛰️

An open, reproducible pipeline that detects year-to-year change in woody
vegetation (trees and shrubs) in the municipality (*partido*) of **Monte
Hermoso**, a coastal area in the south of Buenos Aires Province, Argentina,
using **only free and public data** processed on Google Earth Engine.

> **Status: research prototype.** The pipeline is engineered, fast and
> reproducible, but detection precision has **not** been validated yet: the
> manual review that measures it is the current bottleneck (see
> [Current status](#current-status-and-validation-)).

Every detected event is a **candidate change** in the satellite signal. The
project does not assume it is a loss of trees: reviewing a labeled sample is
what tells which ones are.

## Why it matters 🌊

Monte Hermoso's beach is backed by the *Médanos Blancos* dune field
([municipal site](https://montehermoso.gov.ar/sitio/atractivos/medanos-blancos/)),
and the Pehuen Có–Monte Hermoso Natural Reserve, a 2,000 ha provincial
protected area, lies between Monte Hermoso and Coronel Rosales
([Wikipedia](https://es.wikipedia.org/wiki/Reserva_natural_Pehuen_C%C3%B3-Monte_Hermoso)).

Along the Buenos Aires coast the dunes were widely afforested with non-native
trees: poplars, eucalyptus, Australian acacias, tamarisks and, above all, pines
([Wikipedia](https://es.wikipedia.org/wiki/Dunas_costeras_bonaerenses)). So
"woody cover" here mixes planted exotics, shrubs and urban trees, and a decrease in it is
not automatically ecological damage: it can be urban clearing, plantation
removal or even dune restoration. This project measures **where woody cover
changes** and leaves the interpretation to local managers.

Locally, Ordinance 1,913 of Monte Hermoso (2010) regulates actions that alter
forestation and natural topography, bans altering the dune ridge and requires
replacing three trees for each one removed, as reported by the press
([La Nueva, 2010-08-26](https://www.lanueva.com/nota/2010-8-26-9-0-0-limitaciones-al-retiro-de-arboles));
the ordinance remains in force. An open,
auditable yearly record of where woody cover changes can help managers
cross-check permits and replacement requirements.

## Objectives 🎯

1. Produce a yearly series (2000 to present) of woody-cover change events for
   the whole municipality, as GeoJSON and CSV, with area in m² and hectares.
2. Use free data only, and spend the minimum Earth Engine quota, so that any
   municipality, school or NGO can repeat it.
3. Measure honestly how accurate it is: stratified visual review of detected
   events, plus cross-checks against independent public products.
4. Report trees as a **range/scenario derived from the area of detected change**, never as an
   observed count (10 m pixels cannot resolve individual crowns).
5. Attribute causes (urban development vs. natural) only when evidence
   supports it; otherwise report `desconocido` (unknown).

## Approach: free and open data only 🛰️

| Purpose | Dataset (Earth Engine id) | License | Credit |
|---|---|---|---|
| Imagery, 2015 on (10 m) | Sentinel-2 SR Harmonized (`COPERNICUS/S2_SR_HARMONIZED`) | Copernicus Sentinel data terms | Contains modified Copernicus Sentinel data |
| Imagery, 2000-2014 and gap years (30 m) | Landsat 5/7/8/9 Collection 2 Level-2 | Public domain (USGS) | Courtesy U.S. Geological Survey |
| Cloud masking | Cloud Score+ (`GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED`) | CC BY 4.0 | Pasquarella et al. 2023 |
| Land-cover prior and baseline (2015 on) | Dynamic World v1 (`GOOGLE/DYNAMICWORLD/V1`) | CC BY 4.0 | Brown et al. 2022 |
| Independent cross-check | MapBiomas Pampa Collection 4 | MapBiomas terms (CC BY-SA; their pages are inconsistent) | Project MapBiomas Trinational Pampa |
| Independent cross-check | Hansen Global Forest Change (`UMD/hansen/global_forest_change_2025_v1_13`) | CC BY 4.0 | Hansen et al. 2013 |
| Municipality boundary | IGN Argentina via Georef (datos.gob.ar) | CC BY 4.0 | Servicio Georef – argentina.gob.ar/georef (modified: fields added) |

Full citations are in [NOTICE](NOTICE).

## Pipeline 🏗️

```
Earth Engine: Sentinel-2 / Landsat / Dynamic World / Cloud Score+
   |  1 request per year: cloud-masked summer composite (median, +-30 days of Feb 15)
   v
disk cache (data/cache)  -->  SatelliteImage: reflectance, NDVI/NDBI/NDMI,
   |                          Dynamic World label and tree probability
   v
detection (3 baselines: ndvi_diff | dynamic_world | cva), each refined by
   - stable woody mask (woody in year t-1 and t)
   - persistence check (the decline must still hold in year t+1)
   - clip to the municipality polygon, minimum event area
   v
events.geojson  -->  stratified sample + review_url  -->  precision by stratum
   +-- MapBiomas / Hansen cross-check
```

## Engineering notes 🧭

- **One download per year**, not per tile: exact grid via `computePixels`
  (raw bands as `uint16`, one `uint8` land-cover label, one `uint8` tree
  probability). A full run used to take about 30 minutes and crashed on a
  tile-shape mismatch; now the 10 images for 2015-2024 download in about 2.5
  minutes in total (measured), and a re-run is served from the disk cache with
  no Earth Engine requests for imagery.
- Retry with backoff, request timeouts, and a checkpoint after every processed
  year so a failure never loses finished work.
- Years run in parallel (4 workers, conservative for non-commercial quota).
- Pairs of years with different sensor resolution (30 m vs. 10 m) are skipped,
  because NDVI is not comparable across them.

## Current status and validation 📊

Measured on 2016-2023:

| Refinement stage | Candidate events (`ndvi_diff` + `dynamic_world`) |
|---|---|
| Plain NDVI drop / class transition (82% inside the municipality) | 28,839 |
| + stable woody mask, persistence, clip to municipality (100% inside), minimum area | 4,774 |
| + 60-day cloud-masked composites, tree-probability baseline | 1,692 |

The third baseline, `cva` (a minimal Change Vector Analysis with a statistical
threshold), adds 922 events: 2,614 in total, all inside the municipality.

Fewer candidates does **not** by itself mean higher precision. What we know:

- Early diagnosis showed that most of what a plain NDVI drop flagged was
  grassland or cropland changing with the season, not tree loss; the stages
  above target exactly that.
- Agreement with MapBiomas is about 1-2% precision for all three baselines.
  This is a weak reference here: MapBiomas marks about 4% of the municipality as
  woody at the start of a pair, Dynamic World about 43-51% (it counts
  shrubland), so the two do not describe the same class. We use it only as a
  trend indicator.
- An earlier `cva` variant, calibrated against Dynamic World, found no events
  on the full municipality. It was replaced by the simpler version above, which
  is **uncalibrated**: its precision is unknown until the manual labels exist.
- Cause attribution currently returns `desconocido` for every event.
- The 2000-2014 (Landsat-only) series is supported by the code but has not
  been run end to end, and has no Dynamic World prior.
- **Ground truth does not exist yet.** The pipeline writes a stratified sample
  (`manual_review_sample.csv`, 45 events, with a `review_url` per event). Once
  the `label_true_positive` column is filled (1 = confirmed decrease in woody cover, 0 = false
  positive), `python -m gee_mh.validation --labels <csv>` reports precision per
  baseline and size class with 95% Wilson intervals.

## Planned use of Planet imagery 🌍

We plan to apply to Planet's Education and Research program through a
university affiliation. The pipeline already works without Planet data; high
resolution imagery would address its two biggest limits, resolution and
ground truth:

1. **Ground truth.** Visual review of sampled events on ~3 m imagery to build
   the labeled set that today is the bottleneck.
2. **Smaller events.** The median detected event is about 850 m², roughly 8
   Sentinel-2 pixels but about 90 pixels at 3 m.
3. **Crown calibration.** Sub-10 m imagery to calibrate the area-to-trees
   scenario range in `trees.py`.
4. **Dating and seasonality.** Frequent revisits to date events and separate
   seasonal variation from permanent change.

**Data policy:** this repository does not contain, and will not contain,
Planet imagery or raw Planet pixel values. Anything derived from Planet data
will follow the program's sharing rules and carry the notice
"Image © 20xx Planet Labs PBC". Every result above was produced without Planet
data.

## Quick start 🚀

```bash
pip install -r requirements.txt
earthengine authenticate
# set your own Google Cloud project in config.yaml -> ee_project

export PYTHONPATH=src
python3 -m gee_mh.aoi                 # download the municipality boundary
python3 -m gee_mh.compare_baselines --start-year 2016 --end-year 2023
python3 -m gee_mh.external_validation --full --start-year 2016 --end-year 2023
python3 -m gee_mh.validation --labels data/exports/manual_review_sample.csv
```

Offline self-checks (no Earth Engine needed):
`python3 -m gee_mh.preprocessing`, `gee_mh.detection`, `gee_mh.export`,
`gee_mh.validation`, `gee_mh.external_validation --selftest`.

## Repository layout 📦

| Module | Role |
|---|---|
| `preprocessing.py` | Earth Engine download, composites, indices, disk cache |
| `timeseries.py` | Annual series (parallel), with previous/next year as context |
| `masks.py`, `detection.py` | Land masks, the three baselines, refinement (`detect_pair`) |
| `attribution.py`, `trees.py` | Cause heuristic, tree-count scenario range |
| `export.py`, `validation.py` | GeoJSON/CSV/GeoTIFF, metrics, stratified sampling, precision |
| `aoi.py`, `config.py` | Municipality boundary download, `config.yaml` loading |
| `compare_baselines.py`, `external_validation.py`, `pipeline.py` | Entry points |

## Tools 🛠️

- Python, NumPy, pandas
- Google Earth Engine Python API
- GeoPandas, Shapely, rasterio
- OpenCV (connected components, resampling, smoothing)
- PyYAML

## Skills 🧠

- Remote-sensing time series: cloud-masked seasonal composites from
  Sentinel-2 and Landsat
- Change detection and false-positive analysis: finding out what a detector
  actually flags
- Quota-aware data engineering on Earth Engine: single-request downloads,
  disk cache, retries, checkpoints
- Validation design: stratified sampling, Wilson intervals, independent
  cross-checks
- Reproducible research code: offline self-checks, honest reporting of
  negative results
- Open-data licensing and attribution

## Limitations ⚠️

- Woody cover includes shrubs, dune afforestation and plantations; no free
  layer separates natural forest from planted trees.
- A single summer composite per year can still confuse stress or drought with
  an actual decrease in woody cover.
- 10 m imagery cannot count individual trees.
- Detection quality is unmeasured until the labeled sample exists.

## Acknowledgements and license 📎

The idea and initial design come from the Omdena
[Córdoba, Argentina chapter](https://github.com/OmdenaAI/CordobaArgentinaChapter_MonitoringLandUseTransformation)
project on land-use change detection; no files from that repository are
included here. See [NOTICE](NOTICE). Released under the MIT License
([LICENSE](LICENSE)).
