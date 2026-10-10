"""Archive Tidewatch and other Virginia storm tide forecasts on a 6-hour cycle.

Run it from cron or a scheduled GitHub workflow a few minutes after
00/06/12/18Z; it works out which cycle it belongs to and what is due:

    python twarchive.py run                  # whatever is due now
    python twarchive.py run --cycle 2026-10-07T00Z
    python twarchive.py run --jobs tidewatch:30d --force
    python twarchive.py status

Layout under the data root (--data, default ../data):

    tidewatch/f36/2026/10/2026-10-07T00Z/SWPT_2018_f36_MLLW.csv ...
    tidewatch/3d/..., tidewatch/30d/...
    classic/2026/10/2026-10-07T00Z/...
    nwps/..., nws-cfw/..., extra/<name>/...
    models/<model>/2026/10/2026-10-07T00Z/...   filed by model run, not archive cycle
    index/<job>/<YYYY-MM>.csv   one row per archived file, appended every run
    status.json            last result of every job

Every cycle directory holds manifest.csv. Files that fail a check are kept
and flagged in the manifest rather than deleted.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import sources as cfg

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.environ.get("TWARCHIVE_DATA", HERE.parent / "data"))
CYCLE_HOURS = 6
GZIP_EXTENSIONS = (".html", ".pypickle")
MANIFEST_FIELDS = [
    "cycle", "fetched_utc", "job", "file", "url", "http_status", "bytes",
    "sha256", "last_modified", "status", "note",
]
INDEX_FIELDS = MANIFEST_FIELDS + ["station", "datum", "first_time", "last_time"]
TW_NAME = re.compile(
    r"^(?P<station>[A-Z0-9]+)_\d{4}_(?P<product>f36|3d|30d)(?:_(?P<datum>MLLW|NAVD))?"
)

log = logging.getLogger("twarchive")


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------
SNAP_BEFORE = timedelta(minutes=30)
SNAP_AFTER = timedelta(hours=2, minutes=30)


def nearest_cycle(now):
    """The 6-hour mark a run belongs to. A run from 30 minutes before to 2.5
    hours after a mark (scheduled runners can lag) files under that mark; a run
    at any other time is off-schedule and files under its own minute."""
    base = now.replace(minute=0, second=0, microsecond=0)
    base -= timedelta(hours=base.hour % CYCLE_HOURS)
    for mark in (base, base + timedelta(hours=CYCLE_HOURS)):
        if mark - SNAP_BEFORE <= now <= mark + SNAP_AFTER:
            return mark
    return now.replace(second=0, microsecond=0)


def on_schedule(cycle):
    return cycle.minute == 0 and cycle.hour % CYCLE_HOURS == 0


def cycle_label(cycle):
    """2026-10-07T00Z on schedule; 2026-10-06T2104Z off schedule."""
    return cycle.strftime("%Y-%m-%dT%HZ" if on_schedule(cycle) else "%Y-%m-%dT%H%MZ")


def parse_cycle(text):
    for fmt in ("%Y-%m-%dT%HZ", "%Y-%m-%dT%H%MZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError(f"cycle must look like 2026-10-07T00Z or 2026-10-06T2104Z: {text}")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_session():
    s = requests.Session()
    s.headers["User-Agent"] = cfg.USER_AGENT
    return s


def fetch(session, url, **kw):
    """GET with retries. Returns the response, or raises the last error."""
    last = None
    for attempt in range(cfg.RETRIES):
        try:
            r = session.get(url, timeout=cfg.REQUEST_TIMEOUT, **kw)
            if r.status_code < 500:
                return r
            last = requests.HTTPError(f"HTTP {r.status_code}")
        except requests.RequestException as e:
            last = e
        time.sleep(2 ** (attempt + 1))
    raise last


def list_directory(session, base_url):
    """File URLs from an Apache-style directory listing."""
    r = fetch(session, base_url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("?", "#", "/")) or href.endswith("/") or "://" in href:
            continue
        urls.add(urljoin(base_url, href))
    return sorted(urls)


# ---------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------
def inspect_tidewatch_csv(data, product, cycle):
    """Return (status, note, first_time, last_time) for a Tidewatch CSV."""
    try:
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))))
    except csv.Error as e:
        return "suspect", f"unreadable CSV: {e}", "", ""
    if not rows or "time" not in rows[0]:
        return "suspect", "no time column or no rows", "", ""
    times = [r["time"] for r in rows if r.get("time")]
    first, last = (times[0], times[-1]) if times else ("", "")
    if product != "f36":
        return "ok", "", first, last
    if "px30" not in rows[0]:
        return "suspect", "no px30 column", first, last
    if not any(_is_number(r.get("px30")) for r in rows):
        return "suspect", "px30 empty", first, last
    try:
        end = datetime.fromisoformat(last.replace("Z", "+00:00"))
        if end < cycle:
            return "suspect", f"stale: forecast ends {last}", first, last
    except ValueError:
        pass
    return "ok", "", first, last


def _is_number(v):
    try:
        float(v)
        return v.strip().lower() != "nan"
    except (TypeError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Writing a cycle
# ---------------------------------------------------------------------------
class CycleWriter:
    """Collects one job's files in a temp dir, then moves it into place."""

    def __init__(self, data_root, job, rel_dir, cycle):
        self.job, self.cycle, self.data_root = job, cycle, data_root
        self.final = data_root / rel_dir / cycle.strftime("%Y/%m") / cycle_label(cycle)
        self.tmp = self.final.with_name(self.final.name + ".partial")
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)
        self.rows = []
        self.pack_dir = None

    def add(self, name, url, response=None, data=None, status="ok", note="",
            extra=None, gz=False):
        """Save one file and record it. `response` or `data` carries content."""
        row = dict.fromkeys(INDEX_FIELDS, "")
        row.update(cycle=cycle_label(self.cycle), job=self.job, url=url,
                   fetched_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   status=status, note=note)
        if response is not None:
            row["http_status"] = response.status_code
            row["last_modified"] = _http_date(response.headers.get("Last-Modified"))
            if response.status_code != 200:
                row.update(status="failed", note=f"HTTP {response.status_code}")
                data = None
            else:
                data = response.content
        if data is not None:
            row["sha256"] = hashlib.sha256(data).hexdigest()
            row["bytes"] = len(data)
            if gz or name.endswith(GZIP_EXTENSIONS):
                name += ".gz"
                data = gzip.compress(data, mtime=0)
            (self.tmp / name).write_bytes(data)
        elif row["status"] == "ok":
            row["status"] = "failed"
        row["file"] = name
        row.update(extra or {})
        self.rows.append(row)
        return row

    def commit(self, data_root):
        with (self.tmp / "manifest.csv").open("w", newline="") as f:
            w = csv.DictWriter(f, MANIFEST_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(self.rows)
        if self.final.exists():
            shutil.rmtree(self.final)
        self.tmp.rename(self.final)
        index = index_path(data_root, self.job, self.cycle)
        index.parent.mkdir(parents=True, exist_ok=True)
        new = not index.exists()
        with index.open("a", newline="") as f:
            w = csv.DictWriter(f, INDEX_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            for row in self.rows:
                w.writerow({**row, "file": str((self.final / row["file"]).relative_to(data_root))})
        counts = {}
        for row in self.rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts


def index_path(data_root, job, cycle):
    """Index files are split by month so none grows without bound."""
    return data_root / "index" / job.replace(":", "_") / f"{cycle:%Y-%m}.csv"


def pack_name(data_root, cycle_dir):
    """tidewatch/f36/2026/10/2026-10-07T00Z -> tidewatch_f36_2026-10-07T00Z.tar.gz"""
    rel = cycle_dir.relative_to(data_root)
    return "_".join(rel.parts[:-3]) + f"_{rel.parts[-1]}.tar.gz"


def pack(data_root, cycle_dir, pack_dir):
    """Tar a finished cycle directory into pack_dir and remove the loose files.
    The tarball keeps the data-root-relative paths the index uses."""
    rel = cycle_dir.relative_to(data_root)
    pack_dir.mkdir(parents=True, exist_ok=True)
    out = pack_dir / pack_name(data_root, cycle_dir)
    with tarfile.open(out, "w:gz") as t:
        t.add(cycle_dir, arcname=str(rel))
    shutil.rmtree(cycle_dir)
    return out


def _http_date(value):
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def job_tidewatch(session, w, product, csv_only=False):
    pattern, exts = cfg.TIDEWATCH_PRODUCTS[product]
    alt = cfg.TIDEWATCH_ALT_PATTERNS.get(product)
    if csv_only:
        exts = (".csv",)
    urls = []
    for url in list_directory(session, cfg.TIDEWATCH_URL):
        name = unquote(Path(urlparse(url).path).name)
        if not name.lower().endswith(exts):
            continue
        if pattern in name or (alt and alt in name):
            urls.append((name, url))
    if not urls:
        raise RuntimeError(f"no {product} files listed at {cfg.TIDEWATCH_URL}")
    for name, url in urls:
        m = TW_NAME.match(name)
        extra = {"station": m["station"], "datum": m["datum"] or ""} if m else {}
        try:
            r = fetch(session, url)
        except requests.RequestException as e:
            w.add(name, url, status="failed", note=str(e)[:200], extra=extra)
            continue
        row = w.add(name, url, response=r, extra=extra)
        if row["status"] == "ok" and name.endswith(".csv"):
            status, note, first, last = inspect_tidewatch_csv(r.content, product, w.cycle)
            row.update(status=status, note=note, first_time=first, last_time=last)
        if row["status"] == "ok" and row["last_modified"]:
            age = w.cycle - datetime.fromisoformat(row["last_modified"])
            if age > timedelta(hours=12):
                row.update(note=f"not updated upstream for {age}")


def job_classic(session, w):
    urls = list_directory(session, cfg.CLASSIC_URL)
    kept = 0
    for url in urls:
        name = unquote(Path(urlparse(url).path).name)
        if name in cfg.CLASSIC_SKIP or Path(name).suffix.lower() not in cfg.CLASSIC_EXTENSIONS:
            continue
        kept += 1
        m = re.match(r"^([A-Z0-9]{4})\d{4}", name)
        extra = {"station": m[1]} if m else {}
        try:
            w.add(name, url, response=fetch(session, url), extra=extra)
        except requests.RequestException as e:
            w.add(name, url, status="failed", note=str(e)[:200], extra=extra)
    if not kept:
        raise RuntimeError(f"no files listed at {cfg.CLASSIC_URL}")


def job_nwps(session, w):
    params = {f"bbox.{k}": v for k, v in cfg.NWPS_BBOX.items()}
    params["srid"] = "EPSG_4326"
    url = f"{cfg.NWPS_API}/gauges"
    r = fetch(session, url, params=params)
    r.raise_for_status()
    w.add("gauges.json", r.url, response=r)
    gauges = [g for g in r.json().get("gauges", [])
              if (g.get("pedts") or {}).get("forecast")]
    for g in gauges:
        lid = g["lid"]
        gurl = f"{cfg.NWPS_API}/gauges/{lid}/stageflow"
        try:
            gr = fetch(session, gurl)
        except requests.RequestException as e:
            w.add(f"{lid}_stageflow.json", gurl, status="failed", note=str(e)[:200],
                  extra={"station": lid})
            continue
        if gr.status_code != 200:
            w.add(f"{lid}_stageflow.json", gurl, response=gr, extra={"station": lid})
            continue
        body = trim_nwps(gr.json(), w.cycle)
        fc = body.get("forecast") or {}
        pts = fc.get("data") or []
        extra = {"station": lid, "last_modified": _http_date(gr.headers.get("Last-Modified"))}
        if not pts:
            w.add(f"{lid}_stageflow.json", gurl, status="skipped",
                  note="no forecast points", extra=extra)
            continue
        extra.update(first_time=pts[0].get("validTime", ""), last_time=pts[-1].get("validTime", ""))
        w.add(f"{lid}_stageflow.json.gz", gurl,
              data=gzip.compress(json.dumps(body).encode(), mtime=0),
              note=f"forecast issued {fc.get('issuedTime', '')}", extra=extra)


def trim_nwps(body, cycle, keep_hours=72):
    """NWPS returns about 30 days of observations with each forecast; keep the
    last 3 days, which is all a forecast-versus-observed check needs."""
    obs = body.get("observed") or {}
    if obs.get("data"):
        cutoff = cycle - timedelta(hours=keep_hours)
        obs["data"] = [d for d in obs["data"]
                       if datetime.fromisoformat(d["validTime"].replace("Z", "+00:00")) >= cutoff]
    return body


def job_nws_products(session, w):
    headers = {"Accept": "application/ld+json"}
    found = 0
    for ptype in cfg.NWS_PRODUCT_TYPES:
        for office in cfg.NWS_OFFICES:
            lurl = f"{cfg.NWS_API}/products/types/{ptype}/locations/{office}"
            r = fetch(session, lurl, headers=headers)
            r.raise_for_status()
            # Products issued since the previous cycle (plus slack), newest first.
            since = w.cycle - timedelta(hours=CYCLE_HOURS + 1)
            for p in r.json().get("@graph", []):
                issued = datetime.fromisoformat(p["issuanceTime"].replace("Z", "+00:00"))
                if issued < since:
                    continue
                purl = f"{cfg.NWS_API}/products/{p['id']}"
                pr = fetch(session, purl, headers=headers)
                if pr.status_code != 200:
                    w.add(f"{ptype}_{office}_{p['id']}.txt", purl, response=pr)
                    continue
                text = pr.json().get("productText", "")
                stamp = issued.strftime("%Y%m%dT%H%MZ")
                w.add(f"{ptype}_{office}_{stamp}_{p['id'][:8]}.txt", purl,
                      data=text.encode(), extra={"station": office,
                                                 "first_time": issued.isoformat()})
                found += 1
    if not found:
        # An empty cycle is normal outside events; record that we looked.
        w.add("none_issued.txt", f"{cfg.NWS_API}/products/types",
              data=b"No coastal flood products issued in this window.\n",
              note="no products")


def job_extra(session, w):
    for name, url in cfg.EXTRA_URLS:
        try:
            w.add(name, url, response=fetch(session, url))
        except requests.RequestException as e:
            w.add(name, url, status="failed", note=str(e)[:200])


def cbofs_subset(data):
    """Water level from a CBOFS station netCDF as CSV: a header block giving
    each station's lon/lat, then time,zeta_<n> columns in metres above MSL."""
    import netCDF4  # only needed for this job
    with netCDF4.Dataset("cbofs.nc", memory=data) as d:
        t = netCDF4.num2date(d["ocean_time"][:], d["ocean_time"].units,
                             only_use_cftime_datetimes=False)
        zeta = d["zeta"][:]
        lon, lat = d["lon_rho"][:], d["lat_rho"][:]
    out = io.StringIO()
    out.write("# CBOFS stations water level (zeta, m, MSL). Station n: lon, lat\n")
    for i in range(len(lon)):
        out.write(f"# zeta_{i}: {lon[i]:.5f}, {lat[i]:.5f}\n")
    out.write("time," + ",".join(f"zeta_{i}" for i in range(len(lon))) + "\n")
    for k, tk in enumerate(t):
        row = ["" if v is None or v != v else f"{v:.4f}" for v in zeta[k].filled(float("nan")).tolist()]
        out.write(f"{tk:%Y-%m-%dT%H:%M:%SZ}," + ",".join(row) + "\n")
    return out.getvalue().encode()


def job_models(session, w):
    """Save every model run from the last MODEL_LOOKBACK_HOURS not yet archived.
    Runs are filed by their own cycle under models/<name>/YYYY/MM/<run>/."""
    data_root = w.data_root
    now = min(datetime.now(timezone.utc), w.cycle + timedelta(hours=CYCLE_HOURS / 2))
    start = now - timedelta(hours=cfg.MODEL_LOOKBACK_HOURS)
    saved = 0
    for m in cfg.MODELS:
        run = start.replace(minute=0, second=0, microsecond=0)
        while run <= now:
            if run.hour in m["hours"]:
                dest = data_root / "models" / m["name"] / run.strftime("%Y/%m") / cycle_label(run)
                job = f"models:{m['name']}"
                if not (dest / "manifest.csv").exists() and not indexed(data_root, job, run):
                    saved += _archive_model_run(session, data_root, m, run, w.pack_dir)
            run += timedelta(hours=1)
    w.add("models.txt", "", data=f"{saved} model runs archived\n".encode())


def indexed(data_root, job, cycle):
    """True if the index already lists this job's cycle. Lets a fresh runner
    (GitHub) that only has the index skip runs saved by earlier runs."""
    path = index_path(data_root, job, cycle)
    if path not in _INDEX_CACHE:
        cycles = set()
        if path.exists():
            with path.open(newline="") as f:
                cycles = {r["cycle"] for r in csv.DictReader(f)}
        _INDEX_CACHE[path] = cycles
    return cycle_label(cycle) in _INDEX_CACHE[path]


_INDEX_CACHE = {}


def _archive_model_run(session, data_root, m, run, pack_dir=None):
    fields = dict(ymd=run.strftime("%Y%m%d"), hh=run.strftime("%H"),
                  y=run.strftime("%Y"), m=run.strftime("%m"), d=run.strftime("%d"))
    mw = CycleWriter(data_root, f"models:{m['name']}", f"models/{m['name']}", run)
    for tmpl in m["keys"]:
        key = tmpl.format(**fields)
        url = cfg.S3_URL.format(bucket=m["bucket"], key=key)
        name = Path(key).name
        try:
            r = fetch(session, url)
        except requests.RequestException as e:
            log.warning("%s: %s", url, e)
            break
        if r.status_code in (403, 404):  # not posted yet; try next cycle
            break
        if r.status_code == 200 and m.get("subset") == "cbofs":
            try:
                mw.add(name.replace(".nc", ".zeta.csv.gz"), url,
                       data=gzip.compress(cbofs_subset(r.content), mtime=0),
                       extra={"station": "", "last_modified": _http_date(r.headers.get("Last-Modified"))})
            except Exception as e:
                mw.add(name, url, status="failed", note=f"subset failed: {e}"[:200])
            continue
        # SHEF is plain text; gzip it in memory (about 8x smaller). Writing the
        # raw file and unlinking it failed on the project's shared mount.
        mw.add(name, url, response=r, gz=name.endswith(".shef"))
    if len(mw.rows) < len(m["keys"]):
        shutil.rmtree(mw.tmp, ignore_errors=True)  # incomplete run; retry later
        return 0
    mw.commit(data_root)
    log.info("models %s %s archived", m["name"], cycle_label(run))
    if pack_dir:
        pack(data_root, mw.final, pack_dir)
    return 1


JOBS = {
    "tidewatch:f36": ("tidewatch/f36", lambda s, w: job_tidewatch(s, w, "f36")),
    "tidewatch:3d": ("tidewatch/3d", lambda s, w: job_tidewatch(s, w, "3d")),
    "tidewatch:3d-csv": ("tidewatch/3d", lambda s, w: job_tidewatch(s, w, "3d", csv_only=True)),
    "tidewatch:30d": ("tidewatch/30d", lambda s, w: job_tidewatch(s, w, "30d")),
    "classic": ("classic", job_classic),
    "nwps": ("nwps", job_nwps),
    "nws-cfw": ("nws-cfw", job_nws_products),
    "extra": ("extra", job_extra),
    "models": ("models/_runs", job_models),
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def run(data_root, cycle, jobs, force=False, pack_dir=None):
    session = make_session()
    status_path = data_root / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
    failures = 0
    for job in jobs:
        rel, fn = JOBS[job]
        final = data_root / rel / cycle.strftime("%Y/%m") / cycle_label(cycle)
        packed = pack_dir and (pack_dir / pack_name(data_root, final)).exists()
        done = (final / "manifest.csv").exists() or packed or indexed(data_root, job, cycle)
        if done and not force:
            log.info("%s %s already archived, skipping", job, cycle_label(cycle))
            continue
        w = CycleWriter(data_root, job, rel, cycle)
        w.pack_dir = pack_dir
        started = datetime.now(timezone.utc)
        try:
            fn(session, w)
            counts = w.commit(data_root)
            result = "ok" if not counts.get("failed") else "partial"
            log.info("%s %s: %s", job, cycle_label(cycle), counts)
        except Exception as e:  # one source failing must not stop the others
            log.error("%s %s failed: %s", job, cycle_label(cycle), e)
            counts, result = {"error": str(e)[:300]}, "failed"
            if w.rows:
                w.commit(data_root)
            else:
                shutil.rmtree(w.tmp, ignore_errors=True)
        if pack_dir and w.final.exists():
            log.info("packed %s", pack(data_root, w.final, pack_dir))
        if result != "ok":
            failures += 1
        status[job] = {
            "cycle": cycle_label(cycle),
            "started_utc": started.isoformat(timespec="seconds"),
            "result": result,
            "counts": counts,
        }
    data_root.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    return failures


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="archive root")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="archive one cycle")
    r.add_argument("--cycle", help="e.g. 2026-10-07T00Z (default: nearest to now)")
    r.add_argument("--jobs", help=f"comma list from: {', '.join(JOBS)} (default: what is due)")
    r.add_argument("--force", action="store_true", help="re-fetch a cycle already archived")
    r.add_argument("--pack", type=Path, metavar="DIR",
                   help="tar each finished cycle into DIR and drop the loose files")
    sub.add_parser("status", help="print the last result of every job")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)sZ %(levelname)s %(message)s")
    logging.Formatter.converter = time.gmtime

    if a.cmd == "status":
        path = a.data / "status.json"
        print(path.read_text() if path.exists() else "no runs yet")
        return 0
    cycle = parse_cycle(a.cycle) if a.cycle else nearest_cycle(datetime.now(timezone.utc))
    jobs = a.jobs.split(",") if a.jobs else cfg.jobs_for_cycle(cycle)
    unknown = [j for j in jobs if j not in JOBS]
    if unknown:
        p.error(f"unknown jobs: {unknown}")
    log.info("cycle %s, jobs %s", cycle_label(cycle), jobs)
    return 1 if run(a.data, cycle, jobs, a.force, a.pack) else 0


if __name__ == "__main__":
    sys.exit(main())
