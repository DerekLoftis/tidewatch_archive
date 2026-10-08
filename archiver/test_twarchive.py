"""Offline end-to-end test: serves fake Tidewatch, Classic, NWPS and NWS
endpoints on localhost and runs a full cycle against them.

    python test_twarchive.py
"""

import csv
import gzip
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

CYCLE = datetime(2026, 10, 1, 0, tzinfo=timezone.utc)


def make_site(root):
    tw = root / "tidewatch"
    tw.mkdir()
    times = [CYCLE - timedelta(minutes=24) + timedelta(minutes=6 * i) for i in range(360)]
    for st in ("SWPT", "VB01"):
        for datum in ("MLLW", "NAVD"):
            with open(tw / f"{st}_2018_f36_{datum}.csv", "w") as f:
                f.write("time,pa30,anom,px30\n")
                for t in times:
                    px = "" if st == "VB01" else "1.2"
                    f.write(f"{t:%Y-%m-%dT%H:%M:%SZ},1.5,-0.3,{px}\n")
            (tw / f"{st}_2018_f36_{datum}_hc.html").write_text("<html>chart</html>")
            (tw / f"{st}_2018_f36_{datum}_hc.png").write_bytes(b"\x89PNG")
            for prod in ("3d", "30d"):
                (tw / f"{st}_2018_{prod}_{datum}.csv").write_text("time,feet\n2026-09-30T00:00:00Z,1.0\n")
                (tw / f"{st}_2018_{prod}_{datum}_hc.html").write_text("<html/>")
        (tw / f"{st}_2018_f36.pypickle").write_bytes(b"pickle")
    cl = root / "classic"
    cl.mkdir()
    for n in ("SWPT2010.jpg", "SWPT2010_f36.jpg", "SWPT2010ld.jpg", "SWPT2010_etss.jpg",
              "staout_melt.csv", "staout_1", "logo1c.jpg", "HEADER.html"):
        (cl / n).write_bytes(b"x")


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/nwps/gauges":
            return self.json({"gauges": [
                {"lid": "SWPV2", "pedts": {"observed": "HMIRG", "forecast": "HMIFF"}},
                {"lid": "ACOV2", "pedts": {"observed": "HGIRR", "forecast": ""}},
            ]})
        if p == "/nwps/gauges/SWPV2/stageflow":
            return self.json({"observed": {"data": []}, "forecast": {
                "issuedTime": "2026-09-30T20:00:00Z",
                "data": [{"validTime": "2026-10-01T00:00:00Z", "primary": 3.1},
                         {"validTime": "2026-10-03T00:00:00Z", "primary": 2.0}]}})
        if p.startswith("/nws/products/types/CFW/locations/"):
            office = p.rsplit("/", 1)[1]
            graph = [{"id": f"{office}-new", "issuanceTime": "2026-09-30T21:15:00+00:00"},
                     {"id": f"{office}-old", "issuanceTime": "2026-09-20T21:15:00+00:00"}]
            return self.json({"@graph": graph})
        if p.startswith("/nws/products/"):
            return self.json({"productText": "COASTAL FLOOD ADVISORY\nSewells Point 5.1 ft MLLW"})
        return super().do_GET()

    def json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def main():
    work = Path(tempfile.mkdtemp())
    site = work / "site"
    site.mkdir()
    make_site(site)
    os.chdir(site)
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    os.environ.update(TIDEWATCH_URL=f"{base}/tidewatch/", CLASSIC_URL=f"{base}/classic/",
                      NWPS_API=f"{base}/nwps", NWS_API=f"{base}/nws",
                      S3_URL=base + "/s3/{bucket}/{key}")
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        os.environ.pop(k, None)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import twarchive as tw

    # Cycle snapping
    assert tw.nearest_cycle(datetime(2026, 10, 1, 0, 37, tzinfo=timezone.utc)) == CYCLE
    assert tw.nearest_cycle(datetime(2026, 9, 30, 23, 50, tzinfo=timezone.utc)) == CYCLE
    assert tw.nearest_cycle(datetime(2026, 10, 1, 2, 29, tzinfo=timezone.utc)) == CYCLE
    off = tw.nearest_cycle(datetime(2026, 9, 30, 21, 4, 40, tzinfo=timezone.utc))
    assert tw.cycle_label(off) == "2026-09-30T2104Z", off
    assert tw.parse_cycle("2026-09-30T2104Z") == off
    assert "tidewatch:3d-csv" in __import__("sources").jobs_for_cycle(off)

    data = work / "data"
    rc = tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T00Z"])
    assert rc == 0, rc

    f36 = data / "tidewatch/f36/2026/10/2026-10-01T00Z"
    names = sorted(p.name for p in f36.iterdir())
    assert "SWPT_2018_f36_MLLW.csv" in names
    assert "SWPT_2018_f36_MLLW_hc.html.gz" in names
    assert "SWPT_2018_f36.pypickle.gz" in names
    assert not any(n.endswith(".png") for n in names), names  # charts not archived
    assert gzip.decompress((f36 / "SWPT_2018_f36_MLLW_hc.html.gz").read_bytes()) == b"<html>chart</html>"
    rows = {r["file"]: r for r in csv.DictReader(open(f36 / "manifest.csv"))}
    assert rows["SWPT_2018_f36_MLLW.csv"]["status"] == "ok"
    assert rows["VB01_2018_f36_MLLW.csv"]["status"] == "suspect"  # empty px30, kept
    assert (f36 / "VB01_2018_f36_MLLW.csv").exists()
    assert not any("_3d_" in n for n in names)

    # 00Z on the 1st: full 3d and 30d as well
    d3 = sorted(p.name for p in (data / "tidewatch/3d/2026/10/2026-10-01T00Z").iterdir())
    assert "SWPT_2018_3d_MLLW_hc.html.gz" in d3, d3
    assert (data / "tidewatch/30d/2026/10/2026-10-01T00Z/SWPT_2018_30d_NAVD.csv").exists()

    classic = sorted(p.name for p in (data / "classic/2026/10/2026-10-01T00Z").iterdir())
    assert "SWPT2010_etss.jpg" in classic and "staout_1" in classic
    assert "logo1c.jpg" not in classic and "HEADER.html" not in classic

    nwps = data / "nwps/2026/10/2026-10-01T00Z"
    assert (nwps / "SWPV2_stageflow.json.gz").exists()
    assert not (nwps / "ACOV2_stageflow.json").exists()

    cfw = sorted(p.name for p in (data / "nws-cfw/2026/10/2026-10-01T00Z").iterdir())
    assert len([n for n in cfw if n.startswith("CFW_")]) == 2, cfw  # one per office, old one skipped

    idx = list(csv.DictReader(open(data / "index/tidewatch_f36/2026-10.csv")))
    swpt = [r for r in idx if r["station"] == "SWPT" and r["datum"] == "MLLW" and r["file"].endswith(".csv")]
    assert swpt and swpt[0]["first_time"] == "2026-09-30T23:36:00Z", swpt

    status = json.loads((data / "status.json").read_text())
    assert status["tidewatch:f36"]["result"] == "ok", status

    # Re-running a finished cycle is a no-op; a 06Z cycle takes 3d CSV only.
    assert tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T00Z"]) == 0
    assert tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T06Z"]) == 0
    d3b = sorted(p.name for p in (data / "tidewatch/3d/2026/10/2026-10-01T06Z").iterdir())
    assert all(n.endswith(".csv") for n in d3b), d3b
    assert not (data / "tidewatch/30d/2026/10/2026-10-01T06Z").exists()

    # A source that is down fails alone and is recorded.
    os.environ["CLASSIC_URL"] = f"{base}/missing/"
    import importlib, sources
    importlib.reload(sources)
    tw.cfg = sources
    rc = tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T12Z"])
    status = json.loads((data / "status.json").read_text())
    assert rc == 1 and status["classic"]["result"] == "failed", status
    assert status["tidewatch:f36"]["result"] == "ok"
    # Packing: each cycle becomes one tarball; the index paths resolve inside it.
    import tarfile
    packs = work / "packs"
    assert tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T18Z",
                    "--jobs", "tidewatch:f36", "--pack", str(packs)]) == 0
    tgz = packs / "tidewatch_f36_2026-10-01T18Z.tar.gz"
    assert tgz.exists() and not (data / "tidewatch/f36/2026/10/2026-10-01T18Z").exists()
    members = tarfile.open(tgz).getnames()
    assert "tidewatch/f36/2026/10/2026-10-01T18Z/manifest.csv" in members, members[:3]
    assert tw.main(["--data", str(data), "run", "--cycle", "2026-10-01T18Z",
                    "--jobs", "tidewatch:f36", "--pack", str(packs)]) == 0
    assert not (data / "tidewatch/f36/2026/10/2026-10-01T18Z").exists()  # skipped, not re-fetched
    # Model runs: saved once, skipped later from the index alone (as on GitHub).
    s3 = site / "s3" / "noaa-nos-stofs3d-pds" / "STOFS-3D-Atl" / "stofs_3d_atl.20261001"
    s3.mkdir(parents=True)
    (s3 / "stofs_3d_atl.t12z.points.cwl.shef").write_text(".E SWPV2 20261001 Z DH1200/HMIFC/DIN30/ 3.0\n")
    sources.MODELS = [m for m in sources.MODELS if m["name"] == "stofs3d"]
    sources.MODEL_LOOKBACK_HOURS = 24
    gh = work / "gh"
    for cyc in ("2026-10-01T18Z", "2026-10-02T00Z"):
        tw._INDEX_CACHE.clear()
        assert tw.main(["--data", str(gh), "run", "--cycle", cyc,
                        "--jobs", "models", "--pack", str(packs)]) == 0
    assert (packs / "models_stofs3d_2026-10-01T12Z.tar.gz").exists()
    rows = list(csv.DictReader(open(gh / "index/models_stofs3d/2026-10.csv")))
    assert len(rows) == 1, rows  # second run skipped it
    print("all checks passed")


if __name__ == "__main__":
    main()
