"""What the archiver collects, and when.

Every URL and schedule lives here so a new forecast source is a config change.
Times are UTC. A "cycle" is one of the 00/06/12/18Z archive times.
"""

import os

# Contact string NOAA/NWS ask API clients to send.
USER_AGENT = os.environ.get(
    "TWARCHIVE_USER_AGENT",
    "tidewatch-archive (VIMS Center for Coastal Resources Management)",
)

REQUEST_TIMEOUT = 60
RETRIES = 3

# --------------------------------------------------------------------------
# Tidewatch 2.0/3.0 ("current"): Apache listing of per-station files, e.g.
#   SWPT_2018_f36_MLLW.csv  SWPT_2018_f36_MLLW_hc.html  SWPT_2018_f36_MLLW_hc.png
#   SWPT_2018_f36.pypickle  SWPT_2018_3d_NAVD.csv  SWPT_2018_30d_NAVD_hc.html
# --------------------------------------------------------------------------
TIDEWATCH_URL = os.environ.get(
    "TIDEWATCH_URL", "https://web4.vims.edu/physical/tidewatch/"
)

# Which file types to keep for each product. The _hc.png charts are left out
# (Derek, 2026-10-08): they last changed upstream in 2019-2022 and every run
# saved identical copies. Add ".png" back here to archive them again.
TIDEWATCH_PRODUCTS = {
    "f36": ("_f36_", (".csv", ".html", ".pypickle")),
    "3d": ("_3d_", (".csv", ".html")),
    "30d": ("_30d_", (".csv", ".html")),
}
# Stations not archived (Derek, 2026-10-15): NN01-NN10 and VB01-VB10 have not
# updated in over a year and are slated for removal from web4; USNW, USPF and
# USSV are non-tidal or lack needed data. Matched against the start of each
# file name, for Tidewatch 2.0/3.0 and classic alike.
SKIP_STATIONS = r"^(NN\d\d|VB\d\d|USNW|USPF|USSV)(?![A-Z])"

# pypickle files are named STATION_2018_f36.pypickle (no datum), so they
# match "_f36." rather than "_f36_".
TIDEWATCH_ALT_PATTERNS = {"f36": "_f36."}

# --------------------------------------------------------------------------
# Tidewatch 1.0 ("classic"): Matlab graphics plus the station output tables
# behind them (staout_*), e.g. BISH2010_f36.jpg, BISH2010ld.jpg,
# BISH2010_etss.jpg, staout_melt.csv.
# --------------------------------------------------------------------------
CLASSIC_URL = os.environ.get("CLASSIC_URL", "https://web4.vims.edu/drf/TWgraphics/")
CLASSIC_EXTENSIONS = (".jpg", ".csv", ".png", ".gif", ".txt", "")
# Static page furniture that never changes.
CLASSIC_SKIP = {"logo1c.jpg", "HEADER.html"}

# --------------------------------------------------------------------------
# NWS National Water Prediction Service. Every gauge in this box (Virginia
# tidal waters, the Potomac to DC, and the Maryland bay) that has a forecast
# product gets its stage/flow JSON archived, observed and forecast together.
# --------------------------------------------------------------------------
NWPS_API = os.environ.get("NWPS_API", "https://api.water.noaa.gov/nwps/v1")
NWPS_BBOX = {"xmin": -77.6, "ymin": 36.5, "xmax": -75.2, "ymax": 39.0}

# --------------------------------------------------------------------------
# NWS Coastal Flood products (warnings/advisories/statements) from the
# Wakefield (AKQ, most of Virginia) and Sterling (LWX, Potomac/DC) offices.
# Their tables carry forecast storm tide at named gauges.
# --------------------------------------------------------------------------
NWS_API = os.environ.get("NWS_API", "https://api.weather.gov")
NWS_OFFICES = ("AKQ", "LWX")
NWS_PRODUCT_TYPES = ("CFW",)

# --------------------------------------------------------------------------
# Model point forecasts on NOAA's open-data AWS buckets (see the surge
# guidance survey in surge_guidance/). Each run is archived once, whenever it
# appears; every archive cycle looks back MODEL_LOOKBACK_HOURS for runs it
# has not saved yet, since these post 2 to 9 hours after their cycle time.
# key: template with {ymd} (20261007) and {hh} (00); "{y}/{m}/{d}" also work.
# subset: "cbofs" keeps only water level from the 50 MB station file.
# --------------------------------------------------------------------------
MODEL_LOOKBACK_HOURS = 48
MODELS = [
    {"name": "stofs2d", "bucket": "noaa-gestofs-pds", "hours": (0, 6, 12, 18),
     "keys": ["stofs_2d_glo.{ymd}/stofs_2d_glo.t{hh}z.points.cwl.shef",
              "stofs_2d_glo.{ymd}/stofs_2d_glo.t{hh}z.points.swl.shef",
              "stofs_2d_glo.{ymd}/stofs_2d_glo.t{hh}z.points.htp.shef"]},
    {"name": "stofs3d", "bucket": "noaa-nos-stofs3d-pds", "hours": (12,),
     "keys": ["STOFS-3D-Atl/stofs_3d_atl.{ymd}/stofs_3d_atl.t{hh}z.points.cwl.shef"]},
    {"name": "nwm-medium-coastal", "bucket": "noaa-nwm-pds", "hours": (0, 6, 12, 18),
     "keys": ["nwm.{ymd}/medium_range_coastal_atlgulf_mem1/nwm.t{hh}z.medium_range_coastal.total_water.atlgulf.shef"]},
    {"name": "nwm-short-coastal", "bucket": "noaa-nwm-pds", "hours": (0, 6, 12, 18),
     "keys": ["nwm.{ymd}/short_range_coastal_atlgulf/nwm.t{hh}z.short_range_coastal.total_water.atlgulf.shef"]},
    {"name": "cbofs", "bucket": "noaa-nos-ofs-pds", "hours": (0, 6, 12, 18), "subset": "cbofs",
     "keys": ["cbofs/netcdf/{y}/{m}/{d}/cbofs.t{hh}z.{ymd}.stations.forecast.nc",
              "cbofs/netcdf/{y}/{m}/{d}/cbofs.t{hh}z.{ymd}.stations.nowcast.nc"]},
]
S3_URL = os.environ.get("S3_URL", "https://{bucket}.s3.amazonaws.com/{key}")

# --------------------------------------------------------------------------
# Extra plain files to snapshot every cycle. Add other surge guidance here,
# as (name, url) pairs; each is stored under extra/<name>/.
# --------------------------------------------------------------------------
EXTRA_URLS = []

# --------------------------------------------------------------------------
# Schedule. f36 is Derek's every-6-hour cycle; 3d daily at 00Z; 30d at 00Z on
# the 1st and 15th. The 3d CSVs are also taken every cycle (about 2 MB) so the
# observed record is never more than 6 hours behind an f36 forecast.
# --------------------------------------------------------------------------


def jobs_for_cycle(cycle):
    """Return the job names due at a cycle (a UTC datetime on a 6-hour mark)."""
    jobs = ["tidewatch:f36"]
    # Full 3d set (CSV, html, png) in the 00Z slot; CSV only at the other cycles. A run
    # GitHub started late still counts for its slot (it files under its own minute).
    midnight = cycle.hour < 6
    jobs.append("tidewatch:3d" if midnight else "tidewatch:3d-csv")
    if midnight and cycle.day in (1, 15):
        jobs.append("tidewatch:30d")
    jobs += ["classic", "nwps", "nws-cfw", "models"]
    if EXTRA_URLS:
        jobs.append("extra")
    return jobs
