"""Download fresh price files and refresh the EXISTING truecost.db (no full rebuild).

Examples (from project root):
  .venv/bin/python scripts/update_data.py --list
  .venv/bin/python scripts/update_data.py --download kaleida --ingest kaleida
  .venv/bin/python scripts/update_data.py --download roswell --url https://.../file.json --ingest roswell
  .venv/bin/python scripts/update_data.py --ingest ecmc roswell kaleida     # re-ingest files already in data/
  .venv/bin/python scripts/update_data.py --geocode                         # show lat/lng drift (dry run)
  .venv/bin/python scripts/update_data.py --geocode --apply                 # write corrected lat/lng

Safe to re-run: ingest deletes only that hospital's rows, then inserts them again (inside one transaction).
The three Catholic Health CSVs are 4-8 GB each: download them with --download mercy kenmore msm, then run
scripts/build_db.py for a full rebuild (that is the only supported ingest for them).
"""
import argparse
import json
import sqlite3
import sys
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_db as b  # noqa: E402

try:  # python.org builds on macOS ship without root certs; certifi fixes that
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = None
UA = {"User-Agent": "TrueCost-hackathon/1.0 (price transparency research)"}

# key -> (filename in data/, default URL or None, hospitals it feeds)
SOURCES = {
    "kaleida": (b.KALEIDA_FILE,
                "https://cleverleypteusstatic.blob.core.windows.net/readable/161533232_kaleida-health_standardcharges.json",
                list(b.KALEIDA_SITES)),
    "roswell": (b.ROSWELL_FILE, None, ["Roswell Park"]),
    "ecmc": (b.ECMC_FILE, None, ["ECMC"]),
    "mercy": (b.CMS_FILES["Mercy Hospital of Buffalo"], None, ["Mercy Hospital of Buffalo"]),
    "kenmore": (b.CMS_FILES["Kenmore Mercy Hospital"], None, ["Kenmore Mercy Hospital"]),
    "msm": (b.CMS_FILES["Mount St. Mary's Hospital"], None, ["Mount St. Mary's Hospital"]),
}
ALL_HOSPITALS = {**b.HOSPITALS, **b.KALEIDA_SITES}


def download(key, url_override=None):
    fname, url, _ = SOURCES[key]
    url = url_override or url
    if not url:
        sys.exit(f"No URL known for '{key}'. Find the machine-readable file link on the hospital's price "
                 f"transparency page and pass it with --url.")
    dest = b.DATA / fname
    tmp = dest.with_suffix(dest.suffix + ".part")
    b.DATA.mkdir(exist_ok=True)
    have = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={have}-"} if have else {})})
    b.log(f"downloading {key}: {url}" + (f" (resuming at {have:,})" if have else ""))
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        if have and r.status != 206:  # server ignored Range -> start over
            have = 0
        with open(tmp, "ab" if have else "wb") as f:
            n = have
            while chunk := r.read(1 << 20):
                f.write(chunk)
                n += len(chunk)
                if (n >> 20) % 100 == 0:
                    b.log(f"  {n / 1e6:,.0f} MB")
    if dest.exists():
        dest.rename(dest.with_name(f"{dest.stem}.{time.strftime('%Y%m%d')}.bak{dest.suffix}"))
    tmp.rename(dest)
    b.log(f"saved {dest} ({dest.stat().st_size / 1e6:,.1f} MB)")


def ingest(key):
    fname, _, hospitals = SOURCES[key]
    path = b.DATA / fname
    if not path.exists():
        sys.exit(f"Missing {path} - run --download {key} first.")
    if key in ("mercy", "kenmore", "msm"):
        sys.exit("Catholic Health files are huge; run scripts/build_db.py for a full rebuild.")
    col = b.Collector()
    if key == "ecmc":
        b.read_ecmc(col)
    else:
        b.read_cms_json(col, path, hospitals)
    rows = [r for _, r in col.best.values()]
    if not rows:
        sys.exit(f"No target-code rows parsed from {path.name}; the schema may have changed. DB untouched.")
    con = sqlite3.connect(b.DB)
    with con:  # single transaction: all-or-nothing
        con.execute(f"DELETE FROM prices WHERE hospital IN ({','.join('?' * len(hospitals))})", hospitals)
        con.executemany(f"INSERT INTO prices VALUES ({','.join('?' * len(b.COLS))})",
                        [tuple(r[c] for c in b.COLS) for r in rows])
        con.executemany("INSERT OR REPLACE INTO hospitals VALUES (?,?,?,?)",
                        [(h, *ALL_HOSPITALS[h]) for h in hospitals if h in ALL_HOSPITALS])
    for h, n, codes in con.execute(
            f"SELECT hospital, COUNT(*), COUNT(DISTINCT code) FROM prices WHERE hospital IN "
            f"({','.join('?' * len(hospitals))}) GROUP BY hospital", hospitals):
        b.log(f"{h}: {n} rows, {codes} codes")
    con.close()


def geocode(apply):
    """Look each hospital address up in OpenStreetMap Nominatim (1 request/sec, per its usage policy)."""
    con = sqlite3.connect(b.DB)
    for name, addr, lat, lng in con.execute("SELECT hospital, address, lat, lng FROM hospitals").fetchall():
        q = urllib.parse.urlencode({"q": addr, "format": "json", "limit": 1, "countrycodes": "us"})
        with urllib.request.urlopen(urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?{q}", headers=UA), timeout=30, context=CTX) as r:
            hit = json.load(r)
        time.sleep(1.1)
        if not hit:
            b.log(f"{name}: address not found - check by hand ({addr})")
            continue
        nlat, nlng = float(hit[0]["lat"]), float(hit[0]["lon"])
        km = (((nlat - lat) * 111) ** 2 + ((nlng - lng) * 82) ** 2) ** 0.5
        b.log(f"{name}: stored ({lat}, {lng}) vs OSM ({nlat:.4f}, {nlng:.4f}) -> {km:.2f} km apart")
        if apply and km > 0.2:
            con.execute("UPDATE hospitals SET lat=?, lng=? WHERE hospital=?", (round(nlat, 4), round(nlng, 4), name))
    con.commit()
    con.close()
    if not apply:
        b.log("dry run - add --apply to write corrected coordinates")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="show sources and whether files exist")
    ap.add_argument("--download", nargs="+", choices=SOURCES, metavar="KEY")
    ap.add_argument("--url", help="override URL (use with a single --download key)")
    ap.add_argument("--ingest", nargs="+", choices=SOURCES, metavar="KEY")
    ap.add_argument("--geocode", action="store_true")
    ap.add_argument("--apply", action="store_true", help="with --geocode: write the corrected lat/lng")
    a = ap.parse_args()
    if not b.DB.exists() and (a.ingest or a.geocode):
        sys.exit(f"Missing {b.DB}. Run scripts/build_db.py first.")
    if a.list or not (a.download or a.ingest or a.geocode):
        for k, (f, u, _) in SOURCES.items():
            p = b.DATA / f
            b.log(f"{k:8} {'present ' + format(p.stat().st_size / 1e6, ',.0f') + ' MB' if p.exists() else 'missing'}"
                  f" | default URL: {'yes' if u else 'NO - pass --url'}")
    if a.download:
        if a.url and len(a.download) > 1:
            sys.exit("--url works with exactly one --download key")
        for k in a.download:
            download(k, a.url)
    for k in a.ingest or []:
        ingest(k)
    if a.geocode:
        geocode(a.apply)


if __name__ == "__main__":
    main()
