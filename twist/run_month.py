"""Run the monthly TWiST build inside the tidewatch_archive repo.

Downloads the month's archived captures from this repo's own releases
(archive-YYYY-MM and project-runs-YYYY-MM run tarballs, plus the first days
of the next month for the 30-day observation capture and late-month NWPS
issuances), extracts the Tidewatch f36 CSVs, 30-day observation CSVs and
NWPS stageflow files, builds the month's data with build_month.py, pulls
every previously built month from release twist-data, and writes one
self-contained twist_<station>.html per station holding all months (the
month dropdown opens on the newest), plus a zip and this month's
twist_data_<month>.tar.gz for release twist-data.

Observations: the gauge records from the StormSense observation archive
(obs_*.tar.xz on DerekLoftis/StormSense-archive) when GH_TOKEN can read that
repo, with Tidewatch 30-day CSVs filling anything earlier or missing;
otherwise Tidewatch's own 30-day records alone (aliased gauges then score
against Tidewatch's republished feed, not an independent record).

Usage (in GitHub Actions; needs gh and GH_TOKEN):
  python twist/run_month.py [--month YYYY-MM] [--work /tmp/twist] [--out out]
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "DerekLoftis/tidewatch_archive"
SS_REPO = "DerekLoftis/StormSense-archive"
CYC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2,4}Z$")


def sh(*cmd, ok=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode and not ok:
        sys.exit(f"command failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r


def assets(repo, tag):
    r = sh("gh", "api", f"repos/{repo}/releases/tags/{tag}",
           "--jq", ".assets[] | [.id, .name] | @tsv", ok=True)
    if r.returncode:
        return []
    return [line.split("\t") for line in r.stdout.splitlines() if "\t" in line]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="")
    ap.add_argument("--work", default="/tmp/twist")
    ap.add_argument("--out", default="out")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    month = a.month or (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    nxt = f"{y + (m == 12)}-{m % 12 + 1:02d}"
    W = os.path.abspath(a.work)
    out = os.path.abspath(a.out)
    for d in ("tars", "x", "obs"):
        os.makedirs(os.path.join(W, d), exist_ok=True)
    os.makedirs(out, exist_ok=True)

    # ---- download this month's run tarballs (+ first 3 days of the next month)
    want, asset_lines = [], []
    for tag in (f"archive-{month}", f"project-runs-{month}"):
        for aid, name in assets(REPO, tag):
            asset_lines.append(f"{tag} {name}")
            if name.startswith("run_") or name.startswith("backfill_"):
                want.append((tag, aid, name))
    for aid, name in assets(REPO, f"archive-{nxt}"):
        asset_lines.append(f"archive-{nxt} {name}")
        if re.match(rf"run_{nxt}-0[123]T", name):
            want.append((f"archive-{nxt}", aid, name))
    open(os.path.join(W, "assets.txt"), "w").write(
        "\n".join(line for line in asset_lines
                  if re.search(r"(run_|backfill_)", line)) + "\n")
    print(len(want), "tarballs to fetch")
    for tag, aid, name in want:
        tar = os.path.join(W, "tars", name)
        if not os.path.exists(tar):
            with open(tar, "wb") as f:
                subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                                f"/repos/{REPO}/releases/assets/{aid}"], stdout=f, check=True)
        d = os.path.join(W, "x", name.replace(".tar.gz", "").replace(".tar.xz", ""))
        os.makedirs(d, exist_ok=True)
        flags = "-xJf" if name.endswith(".xz") else "-xzf"
        subprocess.run(["tar", flags, tar, "-C", d, "--wildcards",
                        "*_2018_f36_MLLW.csv", "--wildcards", "*_2018_30d_MLLW.csv",
                        "--wildcards", "*_stageflow.json.gz"],
                       capture_output=True)
        os.remove(tar)

    # flat tarballs (project captures): give their files a cycle directory
    for d in glob.glob(os.path.join(W, "x", "run_*")):
        loose = [p for p in glob.glob(os.path.join(d, "*_2018_f36_MLLW.csv"))]
        if loose:
            cyc = os.path.basename(d)[4:].replace("_project", "")
            cd = os.path.join(d, "cyc", cyc)
            os.makedirs(cd, exist_ok=True)
            for p in loose:
                os.rename(p, os.path.join(cd, os.path.basename(p)))

    # ---- observations: StormSense archive when reachable
    obs_dirs = []
    for tag_month in (month, nxt):
        got = assets(SS_REPO, f"obs-{tag_month}")
        for aid, name in got:
            if not name.startswith("obs_"):
                continue
            if tag_month == nxt and not re.match(rf"obs_{nxt}-0[123]\b", name):
                continue
            tar = os.path.join(W, "tars", name)
            with open(tar, "wb") as f:
                subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                                f"/repos/{SS_REPO}/releases/assets/{aid}"], stdout=f, check=True)
            subprocess.run(["tar", "-xJf", tar, "-C", os.path.join(W, "obs")], capture_output=True)
            os.remove(tar)
    if glob.glob(os.path.join(W, "obs", "**", "6min", "*.csv"), recursive=True):
        obs_dirs = [os.path.join(W, "obs")]
        print("observations: StormSense archive + Tidewatch 30d fallback")
    else:
        print("observations: Tidewatch 30d records only (no StormSense access)")

    # ---- previously built months (release twist-data), so pages accumulate
    diag = os.path.join(W, "diag")
    os.makedirs(diag, exist_ok=True)
    for aid, name in assets(REPO, "twist-data"):
        mm = re.match(r"twist_data_(\d{4}-\d{2})\.tar\.gz$", name)
        if not mm or mm.group(1) == month:
            continue
        tar = os.path.join(W, "tars", name)
        with open(tar, "wb") as f:
            subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                            f"/repos/{REPO}/releases/assets/{aid}"], stdout=f, check=True)
        subprocess.run(["tar", "-xzf", tar, "-C", diag], check=True)
        os.remove(tar)

    # 30d directories: every extracted dir that holds 30d CSVs
    tw30 = sorted({os.path.dirname(p) for p in glob.glob(
        os.path.join(W, "x", "**", "*_2018_30d_MLLW.csv"), recursive=True)})
    print(len(tw30), "30-day capture dirs")

    meta = json.load(open(os.path.join(HERE, "stations_meta.json")))
    stations = ",".join(sorted(meta))
    repo_root = os.path.dirname(HERE)
    cmd = [sys.executable, os.path.join(HERE, "build_month.py"), "--month", month,
           "--stations", stations, "--tw", os.path.join(W, "x"),
           "--tw-index", os.path.join(repo_root, "data", "index", "tidewatch_f36"),
           "--nwps", os.path.join(W, "x"),
           "--nwps-index", os.path.join(repo_root, "data", "index", "nwps"),
           "--obs", obs_dirs[0] if obs_dirs else os.path.join(W, "none"),
           "--tw30", *tw30,
           "--levels", os.path.join(HERE, "levels.json"),
           "--meta", os.path.join(HERE, "stations_meta.json"),
           "--assets", os.path.join(W, "assets.txt"),
           "--models", "TW,NWS", "--out", os.path.join(diag, month)]
    subprocess.run(cmd, check=True, cwd=HERE)

    # the month's built data, published to release twist-data for later months
    subprocess.run(["tar", "-czf", os.path.join(out, f"twist_data_{month}.tar.gz"),
                    "-C", diag, month], check=True)

    month_dirs = sorted(d for d in glob.glob(os.path.join(diag, "*"))
                        if os.path.exists(os.path.join(d, "data", "index.json")))
    print("months on the pages:", [os.path.basename(d) for d in month_dirs])
    for st in sorted(meta):
        page = os.path.join(out, f"twist_{st.lower().replace('-', '_')}.html")
        subprocess.run([sys.executable, os.path.join(HERE, "make_tidewatch_page.py"),
                        "--station", st, "--months", *month_dirs,
                        "--highcharts", os.path.join(HERE, "highcharts.js"),
                        "--tw-logo", os.path.join(HERE, "branding", "tidewatch_logo.png"),
                        "--vims-logo", os.path.join(HERE, "branding", "vims_logo.png"),
                        "--out", page], check=True)
    z = os.path.join(out, "twist_all_stations.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for p in sorted(glob.glob(os.path.join(out, "twist_*.html"))):
            zf.write(p, os.path.basename(p))
    print("built", month, "->", out)


if __name__ == "__main__":
    main()
