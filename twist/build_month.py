"""Build one month of forecast diagnostics: every archived forecast run paired
with the gauge's observations and scored with Derek's skill formula.

Writes <out>/data/<ST>.json (one file per station) and <out>/data/index.json.
The viewer (viewer.html) loads one station of one month at a time.

Inputs are unpacked archive files (all ft MLLW):
  --tw     Tidewatch f36 runs: <dir>/**/<cycle>/<ST>_2018_f36_MLLW.csv
           (tidewatch_archive releases archive-YYYY-MM / project-runs-YYYY-MM)
  --tw-index  tidewatch_archive data/index/tidewatch_f36 folder(s), for each
           run's upstream Last-Modified (publication time) and fetch time
  --nwps   NWPS stageflow captures: <dir>/<cycle>/<LID>_stageflow.json.gz
  --obs    StormSense observation days: <dir>/obs_YYYY-MM-DD/6min/<ST>.csv
  --tw30   Tidewatch 30-day CSVs <ST>_2018_30d_MLLW.csv (observations before
           the StormSense observation archive starts)
  --fore72 StormSense fore72_history.csv.gz files
  --levels StormSense flood levels JSON {ST: {action, minor, ...}} (ft MLLW)

Usage (October 2026):
  python tools/diagnostics/build_month.py --month 2026-10 --stations SWPT,USJT,JLMV,CNRC \
      --tw /tmp/tw --tw-index tidewatch_archive/data/index/tidewatch_f36 --nwps /tmp/nwps \
      --obs /tmp/obs --tw30 /tmp/tw30 --fore72 /tmp/f72_2026-10.csv.gz /tmp/f72_2026-11.csv.gz \
      --levels /tmp/levels.json --out /tmp/diag/2026-10
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import metrics

UTC = timezone.utc
STEP = 6  # minutes
NWS_LID = {"SWPT": "SWPV2", "USJT": "JSFV2", "JLMV": "JLMV2"}
NWS_TO_MLLW = {"SWPV2": 0.0, "JSFV2": 0.0}  # NWPS stage datum MLLW; --meta nws_off extends this
# JLMV2 stage was MLLW until 2026-10-15T13:48Z, NAVD88 (MLLW - 1.69 ft) after.
JLMV_CHANGE, JLMV_NAVD = datetime(2026, 10, 15, 13, 48, tzinfo=UTC), 1.69
TW_REPO = "https://github.com/DerekLoftis/tidewatch_archive/releases/download"
NAVD_ABOVE_MLLW = {"SWPT": 1.61, "USJT": 1.113, "JLMV": 1.69, "CNRC": 0.691}
NAMES = {
    "SWPT": "Sewells Point (NOAA 8638610)",
    "USJT": "James River at Jamestown/Scotland Ferry (USGS 02042770)",
    "JLMV": "James River at Leeward Marina, Newport News (NWPS JLMV2)",
    "CNRC": "Accokeek Creek at Crow's Nest (GreenStream/VIMS)",
}
BANDS = {"TW": [(0, 12), (12, 24), (24, 36)], "NWS": [(0, 12), (12, 36), (36, 72)], "SS": [(0, 12), (12, 36), (36, 72)]}
WINDOW_H = {"TW": 36, "NWS": 72, "SS": 72}


def T(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def ceil_hour(t):
    return t if (t.minute == 0 and t.second == 0) else t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def slot_of(t):
    return t.replace(hour=t.hour // 6 * 6, minute=0, second=0, microsecond=0)


def ms(t):
    return int(t.timestamp() * 1000)


# ---------------------------------------------------------------- observations
def load_obs(st, a, start, end):
    """6-minute series (pd.Series, UTC index) from StormSense days, then the
    Tidewatch 30-day CSV where StormSense has nothing."""
    parts = []
    for f in sorted(glob.glob(os.path.join(a.obs, "obs_*", "6min", f"{st}.csv"))):
        d = pd.read_csv(f)
        parts.append(pd.Series(d.iloc[:, 1].values, index=pd.to_datetime(d.iloc[:, 0], utc=True)))
    ss = pd.concat(parts) if parts else pd.Series(dtype=float)
    # stand-in stations: each site capture carries 3 days of the gauge's own obs
    for dir_ in a.site:
        for f in glob.glob(os.path.join(dir_, f"{st}_2018_3d_MLLW.csv")):
            d = pd.read_csv(f)
            t2 = pd.Series(d["feet"].values, index=pd.to_datetime(d["time"], utc=True))
            parts.append(t2)
    ss = pd.concat(parts) if parts else ss
    ss = ss[~ss.index.duplicated()]
    tw = pd.Series(dtype=float)
    for dir_ in a.tw30:
        for f in glob.glob(os.path.join(dir_, f"{st}_2018_30d_MLLW.csv")):
            d = pd.read_csv(f)
            t2 = pd.Series(d["feet"].values, index=pd.to_datetime(d["time"], utc=True))
            tw = pd.concat([tw, t2[~t2.index.isin(tw.index)]])
    s = pd.concat([ss, tw[~tw.index.isin(ss.index)]]).sort_index()
    s = s[~s.index.duplicated()]
    s = s[(s.index >= start) & (s.index < end)]
    return s.dropna()


def nwps_obs(lid, captures):
    """Observed stage in the NWPS captures (3 days each), to MLLW."""
    rows = {}
    for body in captures:
        for p in (body.get("observed") or {}).get("data") or []:
            v = p.get("primary")
            if v is None or v <= -99:  # NWPS sentinels: -999, -9999
                continue
            rows[T(p["validTime"])] = float(v)
    s = pd.Series(rows).sort_index()
    s.index = pd.DatetimeIndex(s.index)
    return nws_to_mllw(lid, s)


def nws_to_mllw(lid, s):
    if lid == "JLMV2":
        off = np.where(s.index >= JLMV_CHANGE, JLMV_NAVD, 0.0)
        return s + off
    return s + NWS_TO_MLLW.get(lid, 0.0)


# ---------------------------------------------------------------- forecasts
def tidewatch_runs(st, a, start, end):
    """One Tidewatch f36 run per 6-hour cycle: the first fresh publication in it.
    A cycle whose captures only repeat an older run is kept as stale."""
    idx = []
    for d in a.tw_index:
        for f in sorted(glob.glob(os.path.join(d, "*.csv"))):
            for x in csv.DictReader(open(f)):
                if x["station"] == st and x["file"].endswith("_MLLW.csv") and x["status"] == "ok":
                    if x["last_modified"]:
                        run = ceil_hour(T(x["last_modified"]))
                    elif x.get("first_time"):  # Sept 2026 backfill rows carry no Last-Modified;
                        ft = T(x["first_time"])  # the f36 series starts within its run's 6-h slot
                        run = ft.replace(minute=0, second=0, microsecond=0) - timedelta(hours=ft.hour % 6)
                    else:
                        continue
                    idx.append((x["cycle"], T(x["fetched_utc"]), run, x["sha256"]))
    files = {}
    for p in glob.glob(os.path.join(a.tw, "**", f"{st}_2018_f36_MLLW.csv"), recursive=True):
        files[os.path.basename(os.path.dirname(p))] = p
    by_run = {}
    for cyc, fetched, run, sha in idx:
        by_run.setdefault(run, []).append((cyc, fetched))
    runs, seen = [], set()
    t = start
    while t < end:
        e = t + timedelta(hours=6)
        fresh = sorted(r for r in by_run if t <= r < e and any(c in files for c, _ in by_run[r]))
        if fresh:
            r = fresh[0]
            rows_r = by_run[r]
            cyc = next(c for c, _ in rows_r if c in files)
            runs.append(dict(model="TW", slot=t, issued=r, path=files[cyc], cycle=cyc, stale=False,
                             fetches=sorted(f for _, f in rows_r)))
            seen.add(r)
        else:
            caps = [(c, f, r) for c, f, r, _ in idx if t <= f < e and c in files]
            if caps:
                c, f, r = min(caps, key=lambda z: z[1])
                runs.append(dict(model="TW", slot=t, issued=r, path=files[c], cycle=c, stale=True, fetches=[f]))
        t = e
    out = []
    for r in runs:
        d = pd.read_csv(r["path"])
        tt = pd.to_datetime(d["time"], utc=True)
        r["t"] = tt
        r["v"] = d["px30"].to_numpy(float)
        r["extra"] = {"tide": d["pa30"].to_numpy(float), "anom": d["anom"].to_numpy(float)}
        out.append(r)
    return out


def site_runs(st, a, start, end):
    """One run per archived site build (stand-in stations rebuilt by
    tidewatch_forecastmodel each cycle): the capture dir name is the cycle,
    e.g. 2026-11-19T18Z, and every capture is a fresh build."""
    out = []
    for d in a.site:
        cyc = os.path.basename(d.rstrip("/"))
        p = os.path.join(d, f"{st}_2018_f36_MLLW.csv")
        m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})?Z$", cyc)
        if not m or not os.path.exists(p):
            continue
        iss = T(f"{m.group(1)}T{m.group(2)}:{m.group(3) or '00'}:00Z")
        if not (start <= iss < end):
            continue
        month_tag = iss.strftime("%Y-%m")
        out.append(dict(model="TW", slot=slot_of(iss), issued=iss, path=p, cycle=cyc, stale=False,
                        fetches=[], download=f"https://github.com/DerekLoftis/tidewatch_archive/releases/"
                        f"download/site-{month_tag}/site_{cyc}.tar.gz"))
    runs = []
    for r in sorted(out, key=lambda r: r["issued"]):
        d = pd.read_csv(r["path"])
        r["t"] = pd.to_datetime(d["time"], utc=True)
        r["v"] = d["px30"].to_numpy(float)
        r["extra"] = {"tide": d["pa30"].to_numpy(float), "anom": d["anom"].to_numpy(float)}
        runs.append(r)
    return runs


def nws_fetches(lid, dirs):
    """issuedTime -> [fetched_utc of captures holding that issuance] from the nwps indexes."""
    out = {}
    for d in dirs:
        for f in glob.glob(os.path.join(d, "*.csv")):
            for x in csv.DictReader(open(f)):
                if x["station"] == lid and x["status"] == "ok" and "forecast issued" in x["note"]:
                    out.setdefault(x["note"].split()[-1], []).append(T(x["fetched_utc"]))
    return out


def nws_captures(lid, a):
    caps = []
    found = sorted(glob.glob(os.path.join(a.nwps, "*", f"{lid}_stageflow.json.gz"))) or         sorted(glob.glob(os.path.join(a.nwps, "**", f"{lid}_stageflow.json.gz"), recursive=True))
    for p in found:
        try:
            caps.append((os.path.basename(os.path.dirname(p)), json.load(gzip.open(p))))
        except Exception:
            pass
    return caps


def nws_runs(lid, caps, start, end):
    out, seen = [], set()
    for cap_cycle, body in caps:
        fc = body.get("forecast") or {}
        iss = T(fc.get("issuedTime", ""))
        if not iss or iss in seen or not (start <= iss < end):
            continue
        pts = [p for p in fc.get("data") or []
           if p.get("primary") is not None and p["primary"] > -99]  # drop -999/-9999 sentinels
        if len(pts) < 3:
            continue
        seen.add(iss)
        s = pd.Series([float(p["primary"]) for p in pts], index=pd.DatetimeIndex([T(p["validTime"]) for p in pts]))
        s = nws_to_mllw(lid, s)
        out.append(dict(model="NWS", slot=slot_of(iss), issued=iss, t=s.index, v=s.to_numpy(float), stale=False,
                        cycle=iss.strftime("%Y-%m-%dT%H%MZ"), capture=cap_cycle))
    return sorted(out, key=lambda r: r["issued"])


def stormsense_runs(st, a, start, end):
    frames = [pd.read_csv(f) for f in a.fore72]
    d = pd.concat(frames)
    d = d[d.station == st].drop_duplicates(["cycle", "lead_h"])
    out, slots = [], set()
    for cyc, g in d.groupby("cycle", sort=True):
        t0 = T(g.t0.iloc[0])
        if not (start <= t0 < end):
            continue
        sl = slot_of(t0 if len(cyc) != 14 else T(cyc[:13] + ":00Z"))
        if sl in slots:  # one build per cycle: the first
            continue
        slots.add(sl)
        g = g.sort_values("lead_h")
        tt = pd.DatetimeIndex([t0 + timedelta(hours=int(h)) for h in g.lead_h])
        out.append(dict(model="SS", slot=sl, issued=t0, t=tt, v=g.fc_ft.to_numpy(float), stale=False, cycle=cyc))
    return out


# ---------------------------------------------------------------- scoring
def interp_obs(obs, times, max_gap_min=12):
    """Observations at the forecast's times: exact where the times match the
    6-minute record, else linear between neighbours no more than 12 min apart
    (StormSense issues CNRC forecasts at :05, :20, ...)."""
    if obs.empty:
        return pd.Series(np.nan, index=times)
    ot = obs.index.as_unit("ms").asi8.astype(float)
    tt = pd.DatetimeIndex(times).as_unit("ms").asi8.astype(float)
    v = np.interp(tt, ot, obs.to_numpy(float), left=np.nan, right=np.nan)
    j = np.clip(np.searchsorted(ot, tt), 1, len(ot) - 1)
    gap = ot[j] - ot[j - 1]
    exact = np.isin(tt, ot)
    v[~exact & (gap > max_gap_min * 60e3)] = np.nan
    return pd.Series(v, index=times)


def score(run, obs):
    t = pd.DatetimeIndex(run["t"])
    t0 = t[0]
    lead = (t - t0).total_seconds() / 3600.0
    keep = lead <= WINDOW_H[run["model"]] + 1e-6
    fc = pd.Series(run["v"][keep], index=t[keep])
    ob = interp_obs(obs, fc.index)
    ok = ob.notna().to_numpy() & np.isfinite(fc.to_numpy())
    L = lead[keep][ok]
    x, y = ob.to_numpy()[ok], fc.to_numpy()[ok]
    st = {"window_h": WINDOW_H[run["model"]], "coverage": round(float(ok.mean()), 3) if len(ok) else 0.0}
    st.update(metrics.statistics(x, y, L))
    bands = {}
    for lo, hi in BANDS[run["model"]]:
        m = (L >= lo) & (L < hi if hi < WINDOW_H[run["model"]] else L <= hi)
        b = metrics.statistics(x[m], y[m], L[m])
        bands[f"{lo}-{hi}"] = {k: b.get(k) for k in ("N", "Skill", "R2", "Bias", "MAE", "RMSE")}
    if run["model"] != "TW":
        b = metrics.statistics(x[L <= 36], y[L <= 36], L[L <= 36])
        bands["0-36"] = {k: b.get(k) for k in ("N", "Skill", "R2", "Bias", "MAE", "RMSE")}
    st["bands"] = bands
    return st


def back_mean(obs, t, hours):
    """Mean of the observations over the `hours` before t (d24/m30 at issuance);
    None when less than half of the 6-minute record is there."""
    w = obs[(obs.index > t - timedelta(hours=hours)) & (obs.index <= t)]
    if len(w) < hours * 10 * 0.5:
        return None
    return round(float(w.mean()), 3)


def rnd(v, n=3):
    if v is None:
        return None
    if isinstance(v, float):
        return None if not np.isfinite(v) else round(v, n)
    if isinstance(v, dict):
        return {k: rnd(x, n) for k, x in v.items()}
    return v


def pack_series(t, v):
    t = pd.DatetimeIndex(t).as_unit("ms")
    d = np.diff(t.asi8) if len(t) > 1 else np.array([])
    vals = [None if not np.isfinite(x) else round(float(x), 3) for x in v]
    if len(d) and (d == d[0]).all():
        return {"t0": int(t.asi8[0]), "dt": int(d[0]), "v": vals}
    return {"t": [int(x) for x in t.asi8], "v": vals}


def summarize(runs, model):
    rs = [r for r in runs if r["model"] == model and not r["stale"] and r["stats"].get("Skill") is not None]
    if not rs:
        return None
    def mean(k, src=None):
        vals = [(src(r) if src else r["stats"]).get(k) for r in rs]
        vals = [v for v in vals if v is not None]
        return float(np.mean(vals)) if vals else None
    out = {"runs": len(rs), **{k: mean(k) for k in ("Skill", "R2", "Bias", "MAE", "RMSE", "PeakError", "PeakTimeError_h")}}
    out["bands"] = {}
    for b in rs[0]["stats"]["bands"]:
        out["bands"][b] = {k: mean(k, lambda r, b=b: r["stats"]["bands"].get(b, {})) for k in ("Skill", "Bias", "MAE", "RMSE")}
    best = max(rs, key=lambda r: r["stats"]["Skill"])
    worst = min(rs, key=lambda r: r["stats"]["Skill"])
    out["best"], out["worst"] = best["id"], worst["id"]
    return rnd(out)


def load_assets(path):
    """Release assets '<tag> <name>' -> [(datetime, url)]; name run_YYYY-MM-DDTHHMMZ[_project].tar.gz."""
    out = []
    if not path:
        return out
    import re
    for line in open(path):
        tag, name = line.split()
        m = re.match(r"run_(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})Z", name)
        if m:
            y, mo, d, h, mi = map(int, m.groups())
            out.append((datetime(y, mo, d, h, mi, tzinfo=UTC), f"{TW_REPO}/{tag}/{name}"))
        elif name.startswith("backfill_"):
            out.append((None, f"{TW_REPO}/{tag}/{name}"))
    return out


BACKFILL_END = datetime(2026, 10, 7, 22, 12, tzinfo=UTC)  # first per-run GitHub asset


def asset_for(assets, fetched, tol_min=15):
    """The release tarball of the GitHub capture that fetched the file: assets are
    named by their capture's start time, so match the index row's fetched_utc."""
    best = None
    for ta, url in assets:
        if ta is None:
            continue
        d = abs((ta - fetched).total_seconds())
        if d <= tol_min * 60 and (best is None or d < best[0]):
            best = (d, url)
    if best:
        return best[1]
    if fetched <= BACKFILL_END:
        for ta, url in assets:
            if ta is None and "backfill_" + fetched.strftime("%Y-%m") in url:
                return url
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True)
    ap.add_argument("--stations", required=True)
    ap.add_argument("--tw", default="")
    ap.add_argument("--tw-index", nargs="*", default=[])
    ap.add_argument("--nwps", default="")
    ap.add_argument("--nwps-index", nargs="*", default=[], help="nwps index folders, for download links")
    ap.add_argument("--obs", default="")
    ap.add_argument("--tw30", nargs="*", default=[])
    ap.add_argument("--site", nargs="*", default=[], help="archived site-build cycle dirs (stand-in station forecasts + 3d obs)")
    ap.add_argument("--fore72", nargs="*", default=[])
    ap.add_argument("--levels", default="")
    ap.add_argument("--meta", default="", help="JSON {ST: {name, navd}} overriding NAMES/NAVD_ABOVE_MLLW")
    ap.add_argument("--coverage", default="", help="coverage JSON to carry into the index")
    ap.add_argument("--assets", default="", help="release asset list: '<tag> <name>' per line (tidewatch_archive)")
    ap.add_argument("--models", default="TW,NWS,SS", help="which models to include")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    y, m = map(int, a.month.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    levels = json.load(open(a.levels)) if a.levels else {}
    meta = json.load(open(a.meta)) if a.meta else {}
    assets = load_assets(a.assets)
    models = set(a.models.split(","))
    os.makedirs(os.path.join(a.out, "data"), exist_ok=True)
    index = {"month": a.month, "built_utc": datetime.now(UTC).strftime("%Y-%m-%d %H:%MZ"), "datum": "MLLW", "units": "ft",
             "stations": []}
    for st in a.stations.split(","):
        # 31-day lookback so m30 at each issuance has a full month of observations
        obs = load_obs(st, a, start - timedelta(days=31), end + timedelta(days=4))
        runs = []
        if "TW" in models:
            if meta.get(st, {}).get("site"):  # stand-in station: forecasts from archived site builds
                runs += site_runs(st, a, start, end)
            elif a.tw:
                runs += tidewatch_runs(st, a, start, end)
        lid = NWS_LID.get(st) or meta.get(st, {}).get("lid")
        off = meta.get(st, {}).get("nws_off")
        if lid and off is not None and lid not in NWS_TO_MLLW:
            NWS_TO_MLLW[lid] = float(off)
        # obs_nwps in --meta: "fill" = the NWPS gauge record fills gaps in our obs
        # (JLMV: StormSense observes it only from 2026-10-27); "prefer" = the NWPS
        # record IS the truth (USCB/USSC/USNR: Tidewatch's feed is another gauge's).
        onw = meta.get(st, {}).get("obs_nwps") or ("fill" if st == "JLMV" else None)
        if lid and a.nwps and ("NWS" in models or onw):
            caps = nws_captures(lid, a)
            if "NWS" in models:
                runs += nws_runs(lid, caps, start, end)
            if onw:
                no = nwps_obs(lid, [b for _, b in caps]).resample("6min").interpolate(limit=10).dropna()
                if onw == "prefer":
                    obs = obs[~obs.index.isin(no.index)]
                no = no[~no.index.isin(obs.index)]
                obs = pd.concat([obs, no]).sort_index()
                obs = obs[~obs.index.duplicated()]
        if a.fore72 and "SS" in models:
            runs += stormsense_runs(st, a, start, end)
        nwsf = nws_fetches(lid, a.nwps_index) if (lid and a.nwps_index) else {}
        for r in runs:
            r["id"] = f"{r['model']}_{r['issued'].strftime('%Y%m%dT%H%MZ')}"
            r["stats"] = score(r, obs)
            fetches = r.get("fetches", [])
            if r["model"] == "NWS":
                fetches = sorted(nwsf.get(r["issued"].strftime("%Y-%m-%dT%H:%M:%SZ"), []))
            r["download"] = r.get("download") or next((u for u in (asset_for(assets, f) for f in fetches) if u), None)
            r["d24"] = back_mean(obs, r["issued"], 24)
            r["m30"] = back_mean(obs, r["issued"], 24 * 30)
        # observations on a regular 6-minute grid over the month (+ margins)
        grid = pd.date_range(start - timedelta(hours=12), end + timedelta(days=3), freq="6min", inclusive="left")
        og = obs.reindex(grid)
        doc = {
            "station": st, "name": NAMES.get(st) or meta.get(st, {}).get("name") or st,
            "month": a.month, "datum": "MLLW", "units": "ft",
            "levels": levels.get(st),
            "navd_offset_ft": NAVD_ABOVE_MLLW.get(st, meta.get(st, {}).get("navd")),
            "obs": pack_series(grid, og.to_numpy(float)),
            "runs": [{
                "id": r["id"], "model": r["model"], "slot": r["slot"].strftime("%Y-%m-%dT%HZ"),
                "issued": r["issued"].strftime("%Y-%m-%dT%H:%MZ"), "stale": r["stale"], "source": r.get("cycle"),
                "download": r.get("download"), "d24": r.get("d24"), "m30": r.get("m30"),
                "fc": pack_series(r["t"], r["v"]), "stats": rnd(r["stats"]),
            } for r in sorted(runs, key=lambda r: (r["model"], r["issued"]))],
        }
        for r, rr in zip(sorted(runs, key=lambda r: (r["model"], r["issued"])), doc["runs"]):
            if "extra" in r:
                rr["tide"] = pack_series(r["t"], r["extra"]["tide"])["v"]
        summ = {mdl: summarize(runs, mdl) for mdl in ("TW", "NWS", "SS")}
        doc["summary"] = {k: v for k, v in summ.items() if v}
        json.dump(doc, open(os.path.join(a.out, "data", f"{st}.json"), "w"), separators=(",", ":"))
        n = {mdl: sum(1 for r in runs if r["model"] == mdl and not r["stale"]) for mdl in ("TW", "NWS", "SS")}
        index["stations"].append({"station": st, "name": doc["name"], "runs": n, "summary": doc["summary"],
                                  "obs_points": int(og.notna().sum())})
        print(st, n, {k: (v or {}).get("Skill") for k, v in doc["summary"].items()})
    if a.coverage:
        index["coverage"] = json.load(open(a.coverage))
    # synthesize a Tidewatch coverage table from the runs for stations the file lacks
    cov = index.setdefault("coverage", {})
    for st in a.stations.split(","):
        if st in cov:
            continue
        doc = json.load(open(os.path.join(a.out, "data", f"{st}.json")))
        by_slot = {r["slot"]: r for r in doc["runs"] if r["model"] == "TW"}
        rows, t = [], start
        while t < end:
            r = by_slot.get(t.strftime("%Y-%m-%dT%HZ"))
            tw = {"state": "missing"}
            if r:
                iss = T(r["issued"].replace("Z", ":00Z"))
                tw = {"state": "stale"} if r["stale"] else \
                     {"state": "ok", "ontime": iss == t, "run": iss.strftime("%H")}
            rows.append({"slot": t.isoformat(), "tw": tw})
            t += timedelta(hours=6)
        cov[st] = rows
    json.dump(index, open(os.path.join(a.out, "data", "index.json"), "w"), separators=(",", ":"))


if __name__ == "__main__":
    main()
