"""Make the self-contained TideWatch diagnostics page for one station.

Inlines Highcharts, the month data from build_month.py, and both logos, so the
file runs offline (an incognito window with no network). Suitable for hosting
on the VIMS site and embedding in an iframe; add ?datum=NAVD (or MLLW) and
?units=m (or ft) to the URL to set the default view, e.g.
    TideWatch_diagnostics_SWPT.html?datum=NAVD

Usage:
  python tools/diagnostics/make_tidewatch_page.py --station SWPT \
      --months /tmp/diag_tw/2026-10 [more month dirs ...] \
      --highcharts stormsense/data/jslib/highcharts.js \
      --tw-logo logo.png --vims-logo vims.png --out TideWatch_diagnostics_SWPT.html
"""
import argparse
import base64
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def data_uri(path):
    return "data:image/png;base64," + base64.b64encode(open(path, "rb").read()).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", required=True)
    ap.add_argument("--months", nargs="+", required=True, help="month output dirs from build_month.py")
    ap.add_argument("--highcharts", required=True)
    ap.add_argument("--tw-logo", required=True)
    ap.add_argument("--vims-logo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--body-only", action="store_true", help="omit the doctype/html wrapper (for an Artifact)")
    a = ap.parse_args()

    months = {}
    for d in a.months:
        sf = os.path.join(d, "data", f"{a.station}.json")
        if not os.path.exists(sf):  # station not built that month
            continue
        idx = json.load(open(os.path.join(d, "data", "index.json")))
        st = json.load(open(sf))
        months[idx["month"]] = {"built_utc": idx["built_utc"], "station": st,
                                "coverage": (idx.get("coverage") or {}).get(a.station)}
    data = json.dumps({"station": a.station, "months": months}, separators=(",", ":"))

    body = open(os.path.join(HERE, "tidewatch_page_body.html")).read()
    body = (body.replace("__TITLE__", f"TWiST {a.station} Diagnostics")
                .replace("__TW_LOGO__", data_uri(a.tw_logo))
                .replace("__VIMS_LOGO__", data_uri(a.vims_logo))
                .replace("__HIGHCHARTS__", open(a.highcharts).read())
                .replace("__DATA__", data))
    if not a.body_only:
        body = "<!doctype html>\n<html lang=\"en\">\n<head>" + body.replace(
            "<meta charset=\"utf-8\">", "<meta charset=\"utf-8\">", 1)
        # head/body split: everything up to </style> stays in head
        body = body.replace("</style>\n", "</style>\n</head>\n<body>\n", 1) + "\n</body>\n</html>\n"
    with open(a.out, "w") as f:
        f.write(body)
    print(a.out, f"{os.path.getsize(a.out)/1e6:.1f} MB", "months:", sorted(months))


if __name__ == "__main__":
    main()
