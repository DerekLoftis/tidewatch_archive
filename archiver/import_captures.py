"""Copy earlier hand or script captures of Tidewatch CSVs into the archive.

    python import_captures.py lexie path/to/extracted/archive_pipeline
    python import_captures.py labelled path/to/derek_sept2026_noreaster --who "Derek's manual captures"

lexie:    <root>/archive/current/<product>/<YYYY-MM-DD_HHMMZ>/*.csv
labelled: <root>/<product>/<YYYY-MM-DDTHHMMZ>[_anything]/*.csv

Snapshots taken exactly on a 6-hour mark keep the normal cycle label
(2026-09-18T00Z); off-schedule ones keep their minutes (2026-09-16T1502Z).
Her pipeline deleted files that failed its checks, so those are absent.
"""

import argparse
import csv
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import twarchive as tw


def main():
    p = argparse.ArgumentParser()
    p.add_argument("layout", choices=["lexie", "labelled"])
    p.add_argument("root", type=Path)
    p.add_argument("--who", default="Lexie Chen's pipeline")
    p.add_argument("--data", type=Path, default=tw.DEFAULT_DATA)
    a = p.parse_args()
    if a.layout == "lexie":
        snaps = sorted((a.root / "archive" / "current").glob("*/20*Z"))
        fmt, cut = "%Y-%m-%d_%H%MZ", 16
    else:
        snaps = sorted(a.root.glob("*/20*Z*"))
        fmt, cut = "%Y-%m-%dT%H%MZ", 16
    for snap in snaps:
        product = snap.parent.name
        t = datetime.strptime(snap.name[:cut], fmt).replace(tzinfo=timezone.utc)
        on_cycle = t.minute == 0 and t.hour % tw.CYCLE_HOURS == 0
        label = tw.cycle_label(t) if on_cycle else t.strftime("%Y-%m-%dT%H%MZ")
        dest = a.data / "tidewatch" / product / t.strftime("%Y/%m") / label
        if dest.exists():
            print("exists, skipping", dest)
            continue
        old = {}
        if (snap / "manifest.csv").exists():
            old = {r["filename"]: r for r in csv.DictReader(open(snap / "manifest.csv"))}
        dest.mkdir(parents=True)
        rows = []
        for f in sorted(snap.glob("*.csv")):
            if f.name == "manifest.csv":
                continue
            shutil.copy2(f, dest / f.name)
            data = f.read_bytes()
            status, note, first, last = tw.inspect_tidewatch_csv(data, product, t)
            m = tw.TW_NAME.match(f.name)
            o = old.get(f.name, {})
            rows.append({
                "cycle": label, "fetched_utc": o.get("archive_time_utc", t.isoformat()),
                "job": f"tidewatch:{product}", "file": f.name,
                "url": o.get("url", tw.cfg.TIDEWATCH_URL + f.name), "http_status": 200,
                "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "last_modified": "", "status": status,
                "note": (f"imported from {a.who} ({snap.name}); " + note).rstrip("; "),
                "station": m["station"] if m else "", "datum": (m["datum"] or "") if m else "",
                "first_time": first, "last_time": last,
            })
        with open(dest / "manifest.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, tw.MANIFEST_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        index = tw.index_path(a.data, f"tidewatch:{product}", t)
        index.parent.mkdir(parents=True, exist_ok=True)
        new = not index.exists()
        with open(index, "a", newline="") as fh:
            w = csv.DictWriter(fh, tw.INDEX_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({**r, "file": str((dest / r["file"]).relative_to(a.data))})
        print(f"{label} {product}: {len(rows)} files")


if __name__ == "__main__":
    main()
