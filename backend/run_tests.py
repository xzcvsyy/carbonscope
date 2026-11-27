"""
CarbonScope non-functional test runner (stdlib only, no installs needed).

1. Start the backend:   uvicorn app.main:app
2. Run this script:     python run_tests.py
   With spam test:      python run_tests.py --spam      (adds 200 extra rows)

NOTE: this script SAVES test rows to activity_records (about 20 rows,
or about 220 with --spam). The start time is printed so you can delete them
afterwards in Supabase (submitted_at >= start time).
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8000"
VALID = dict(transport_subtype="car_gasoline", distance_km=85, electricity_kwh=35,
             device_hours_per_day=8, paper_sheets=10, food_subtype="mixed_diet")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, text = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode()
    except Exception as e:
        return None, str(e), time.perf_counter() - start
    elapsed = time.perf_counter() - start
    try:
        payload = json.loads(text)
    except Exception:
        payload = text
    return status, payload, elapsed


def count():
    s, p, _ = call("GET", "/api/accumulated")
    return p["records_count"] if s == 200 else None


def post(**over):
    body = {**VALID, **over}
    return call("POST", "/api/estimate", {k: v for k, v in body.items() if v != "__omit__"})


print("Start time (UTC):", datetime.now(timezone.utc).isoformat())
s, p, _ = call("GET", "/")
if s != 200:
    sys.exit(f"Backend not reachable at {BASE}: {p}")
print("Records at start:", count(), "\n")

# P-01 -----------------------------------------------------------------
times = []
for _ in range(10):
    s, _, t = post()
    times.append(t if s == 200 else float("nan"))
ok = [t for t in times if t == t]
print(f"P-01 estimate x10: ok={len(ok)}/10  avg={sum(ok)/max(len(ok),1):.3f}s  max={max(ok, default=0):.3f}s")

# P-02 -----------------------------------------------------------------
n = count()
for path in ("/api/report", "/api/accumulated", "/api/anomalies"):
    s, p, t = call("GET", path)
    print(f"P-02 GET {path}: status={s}  time={t:.3f}s  (records in DB: {n})")

# S-01 -----------------------------------------------------------------
before = count()
inj = ["x' OR '1'='1", "<script>alert(1)</script>", "'; DROP TABLE activity_records;--"]
res = []
for v in inj:
    res.append(post(transport_subtype=v)[0])
    res.append(post(food_subtype=v)[0])
after = count()
print(f"S-01 injection statuses={res}  rows saved={after - before}  (expect all 404, 0 saved)")

# S-02 (optional) ------------------------------------------------------
if "--spam" in sys.argv:
    codes = {}
    for _ in range(200):
        s, _, _ = post()
        codes[s] = codes.get(s, 0) + 1
    print(f"S-02 200 rapid submissions, status counts={codes}  (429 would mean rate limiting)")
else:
    print("S-02 skipped (run with --spam)")

# R-02 -----------------------------------------------------------------
before = count()
post()
after = count()
s, p, _ = call("GET", "/api/report")
print(f"R-02 count before={before} after={after}  report.records_collected={p.get('records_collected')}")

# B-01 -----------------------------------------------------------------
bad = [("distance_km", -1), ("distance_km", 301), ("electricity_kwh", 151),
       ("device_hours_per_day", 24.5), ("paper_sheets", 101),
       ("distance_km", "abc"), ("distance_km", None), ("distance_km", "nan"),
       ("distance_km", "inf"), ("distance_km", "__omit__")]
bad_codes = []
before = count()
for field, v in bad:
    bad_codes.append((field, v, post(**{field: v})[0]))
saved = count() - before
print("B-01 invalid inputs (expect all 422):")
for f, v, c in bad_codes:
    print(f"      {f}={v!r} -> {c}")
print(f"      rows saved by invalid inputs: {saved} (expect 0)")
good = [("distance_km", 0), ("distance_km", 300), ("electricity_kwh", 150),
        ("device_hours_per_day", 24), ("paper_sheets", 100)]
print("B-01 boundary inputs (expect all 200):", [post(**{f: v})[0] for f, v in good])

# B-02 -----------------------------------------------------------------
s1, p1, _ = call("GET", "/api/report")
s2, p2, _ = call("GET", "/api/anomalies")
print(f"B-02 report status={s1} has_mining_run={p1.get('has_mining_run')}  "
      f"anomalies status={s2} has_mining_run={p2.get('has_mining_run')}")

print("\nRecords at end:", count())
print("Delete test rows in Supabase where submitted_at >= the start time above.")
