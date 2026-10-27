# Tidewatch latest: site builder

Builds the newest Tidewatch storm tide forecasts (f36) and hindcasts (3d, 30d),
chart pages and CSVs in MLLW and NAVD88, for the public site
https://derekloftis.github.io/tidewatch_latest/ at fixed addresses that are
overwritten each update:

    https://derekloftis.github.io/tidewatch_latest/                      index
    https://derekloftis.github.io/tidewatch_latest/f36/SWPT_2018_f36_MLLW_hc.html
    https://derekloftis.github.io/tidewatch_latest/3d/SWPT_2018_3d_NAVD.csv
    https://derekloftis.github.io/tidewatch_latest/sources.csv            sources of the charts built here
    https://derekloftis.github.io/tidewatch_latest/obs/floodvision/USLN-X_2026-10.csv
    https://derekloftis.github.io/tidewatch_latest/obs/greenstream/CNRC_2026-10.csv

`publish.py` copies web4's files (https://web4.vims.edu/physical/tidewatch/)
under Tidewatch's own names, then `noaa_override.py` builds the stations
Tidewatch lacks or gets wrong, and `styler/` restyles the chart pages (datum
lines HAT to LAT and NAVD88, the m30 line, disclaimer and VIMS logo; libraries
served from `jslib/`, so no CDN is needed). The workflow
`.github/workflows/tidewatch_site.yml` in this repository runs it every 6 hours
and publishes the result to DerekLoftis/tidewatch_latest.

## Charts built here

* CBBT, MNPT, DAHL, WADC: rebuilt from NOAA CO-OPS 6-minute observations
  (27-constituent harmonic tide in `harmonics/`, refit with
  `python noaa_override.py --hourly harmonics`).
* BRDH: the VIMS radar gauge on THREDDS.
* USLN-X (FloodVISION-AI webcam) and CNRC (GreenStream/VDEM sensor): from the
  archives `floodvision.py` and `greenstream.py` keep in `floodvision/` and
  `greenstream/` (published as `obs/` on the site, which StormSense reads).
* Stand-ins for about 37 gauges the VIMS station pages link but web4 has never
  published (VDEM, StormSense, USGS, county and NOAA gauges such as SLIM, SGSM,
  CRSM, WCKV, WSPV, JLMV, GTND), from the NWS NWPS API: the gauge's stage moved
  to MLLW, a harmonic tide fitted to the record (13 constituents until 6 months
  are archived), and Tidewatch's 36-hour forecast with the surge of the nearest
  P-ETSS point. A stand-in steps aside as soon as web4 publishes the station.
  `nwps_stations.csv` holds, per station, the NWPS id, the datum its stage is
  on, the offset to MLLW (with the time of any datum change), NAVD88 above MLLW,
  the P-ETSS point and how the datum was checked. Their tidal datums for the
  chart lines are in `styler/stations_usgs/_datums/`. `nwps_capture.py` archives
  their hourly stage in `nwps/<ST>_<YYYY-MM>.csv`. GNTD repeats GTND.

Charts built here keep Tidewatch's layout and file names: a small ⓘ after the
title shows the data sources as a subtitle when the title is clicked, so the
chart's height in an iframe doesn't change. `sources.csv` on the site lists the
same text per station; the chart CSVs keep Tidewatch's columns.

    python publish.py [site_dir] [raw_dir]

Disclaimer: Tidewatch Charts(TM) are automated storm tide forecasts; User assumes all risk.
