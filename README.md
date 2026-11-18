# Tidewatch forecast archive

Saves every Tidewatch forecast, and the other Virginia storm tide forecasts
alongside it, on a fixed UTC schedule so past forecasts can be scored against
what the water actually did. Builds on Lexie Chen's `archive_pipeline`
(September 2026); her captures are imported under `data/`.

## What is archived, and when

| Source | What | When (UTC) |
|---|---|---|
| Tidewatch 2.0/3.0 `web4.vims.edu/physical/tidewatch/` | f36 forecast: CSV, chart html, `.pypickle`, every station, MLLW and NAVD | 00, 06, 12, 18 |
| | 3d hindcast: CSV, html | 00 daily |
| | 3d hindcast CSV only | 06, 12, 18 (keeps observations within 6 h of each f36) |
| | 30d: CSV, html | 00 on the 1st and 15th |
| Tidewatch Classic `web4.vims.edu/drf/TWgraphics/` | every station jpg (30 h, f36, 3-day, ETSS) and the `staout_*` tables | 00, 06, 12, 18 |
| NWS NWPS `api.water.noaa.gov/nwps/v1` | forecast plus the last 3 days of observations (gzipped JSON) for every gauge between Cape Charles, DC and Bishops Head that has forecast points that cycle | 00, 06, 12, 18 |
| NWS coastal flood products (`CFW`, Wakefield and Sterling offices) | full product text issued since the last cycle | 00, 06, 12, 18 |

| Model point forecasts on NOAA's AWS buckets (from the surge guidance survey) | STOFS-2D-Global (total, surge-only, tide-only SHEF), STOFS-3D-Atlantic (12Z), NWM coastal medium and short range SHEF, CBOFS station water level (subset to CSV from the 50 MB netCDF) | every run, picked up within 6 h of posting; filed by model run under `models/` |

New sources are a line in `archiver/sources.py` (`MODELS`, `EXTRA_URLS`, or a new job).
ETSS and P-ETSS come from NOMADS and are not wired in yet.
The model files add roughly 150 MB a month.

The Tidewatch `_hc.png` charts are no longer saved (since 2026-10-08): they
have not changed upstream since 2019-2022. Add `.png` back to
`TIDEWATCH_PRODUCTS` in `archiver/sources.py` to save them again.

Since 2026-10-15 the retired stations NN01-NN10 and VB01-VB10 and the
non-tidal USNW, USPF and USSV are skipped (`SKIP_STATIONS` in
`archiver/sources.py`), and their earlier files were removed from the project copy.

## Layout

```
data/
  tidewatch/f36/2026/10/2026-10-07T00Z/SWPT_2018_f36_MLLW.csv ... manifest.csv
  tidewatch/3d/...   tidewatch/30d/...
  classic/2026/10/2026-10-07T00Z/...
  nwps/...   nws-cfw/...
  models/stofs2d/2026/10/2026-10-07T00Z/stofs_2d_glo.t00z.points.cwl.shef.gz ...
  index/tidewatch_f36/2026-10.csv     one row per file: cycle, station, datum, path,
                                      first/last forecast time, status, sha256, ...
  status.json                         last result of every job
```

* Cycle folders are named for the scheduled cycle, not the minute the run
  happened: a run from 30 minutes before to 2.5 hours after a 6-hour mark files
  under that mark. A run at any other time is off-schedule and keeps its
  minutes (`2026-10-06T2104Z`), so it never takes the place of a scheduled cycle.
* Html, `.pypickle` and SHEF files are gzipped (`.html.gz`); Tidewatch CSVs are left plain.
* Nothing is deleted. A CSV that fails a check is kept with `status=suspect`
  and the reason in `note` (for example `px30 empty`, or `stale: forecast ends ...`).
* Earlier captures were imported with `archiver/import_captures.py`: Lexie's
  pipeline (14 snapshots, 16-23 Sept) and Derek's manual nor'easter captures
  (24-29 Sept, plus a 30d taken 4 Oct). Their `note` says where they came from.

To find every SWPT f36 forecast in MLLW, read `data/index/tidewatch_f36/*.csv`
and filter `station == "SWPT"`, `datum == "MLLW"`, `file` ending `.csv`. On
GitHub, download the month's tarballs from Releases and extract them into
`data/` first (`gh release download archive-2026-10 -D packs`, then
`for f in packs/*.tar.gz; do tar xzf "$f" -C data; done`).

## Running it

```sh
python3 -m venv .venv && .venv/bin/pip install -r archiver/requirements.txt
.venv/bin/python archiver/twarchive.py run            # whatever is due now
.venv/bin/python archiver/twarchive.py run --cycle 2026-10-07T00Z --jobs tidewatch:30d
.venv/bin/python archiver/twarchive.py status
.venv/bin/python archiver/test_twarchive.py           # offline end-to-end test
```

Schedule it either way:

* **GitHub (this repository):** `.github/workflows/archive.yml` runs at :07
  past each cycle. Everything one run saves goes into a single tarball,
  `run_<time>.tar.gz`, attached to that month's release (`archive-2026-10`).
  Paths inside the tarball match the `file` column of `data/index`. The
  repository itself only commits `data/index` and `data/status.json`, so it
  stays small. To fill a missed cycle while Tidewatch still shows it, run the
  workflow by hand from the Actions tab and give it the cycle. The
  `backfill_*.tar.gz` assets hold everything captured before the move to GitHub,
  from 16 September 2026 on.
* **cron:** see `archiver/crontab.example`; data stays as plain folders.

A run that is repeated for a cycle already archived (in the index or on disk)
does nothing unless `--force` is given. If one source is down, the others still run; the exit
code is 1 and `status.json` says which failed.

## Known gaps upstream (from the September captures)

* 23 Tidewatch stations publish an f36 CSV with no `px30` forecast column
  values: CBBT, MNPT, USLI, NN01-NN10 and VB01-VB10. CBBT's file still holds
  data from May 2022.
* Per cycle, roughly: f36 13 MB (pypickle 7, csv 3, html 2), Classic
  7 MB, full 3d 6 MB, models and NWPS a few MB. About 2.7 GB a month
  (3.5 GB while the png charts were saved).
* Tidewatch updates the f36 files hourly (each starts at :36 past the previous
  hour), so a 6-hourly archive keeps one forecast in six.

## The public Tidewatch latest site

The site at https://derekloftis.github.io/tidewatch_latest/ (the Tidewatch
charts plus the CO-OPS rebuilds, BRDH, USLN-X, CNRC and the NWPS stand-ins) is
built in the private DerekLoftis/tidewatch_forecastmodel since 2026-11-18,
The capture code runs there too, but the
observations it captures stay here, in `data/obs/`:

* `data/obs/floodvision/USLN-X_YYYY-MM.csv`: FloodVISION-AI webcam water level at Little Neck Creek (ft NAVD88)
* `data/obs/greenstream/CNRC_YYYY-MM.csv`: GreenStream/VDEM sensor at Accokeek Creek, Crow's Nest
* `data/obs/nwps/<ST>_YYYY-MM.csv`: hourly NWPS stage for the stand-in stations Tidewatch doesn't publish

Every capture since September 2026 is kept; nothing is shortened. This repository holds no model code.
