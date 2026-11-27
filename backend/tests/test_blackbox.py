"""
Black-box tests: behaviour is judged from inputs and outputs only
(no knowledge of the code's internal paths).

  1. Boundary Value Analysis (BVA)  - 4 bounded numeric inputs + n = 4, 5, 6
  2. Equivalence Partitioning (EP)  - valid / invalid classes for the 5 categories
  3. Interface testing              - web form -> API -> Supabase -> mining process
                                      (field names, data types, bounds, order)

The frontend/ and database/ folders are read as plain files for the interface
checks; those tests are skipped if the folders are not next to backend/.
"""
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import mining
from app.main import app
from app.mining import FEATURE_COLUMNS, preprocess_data, run_mining_job, transform_data

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[2]          # folder containing backend/, frontend/, database/

VALID = dict(transport_subtype="car_gasoline", distance_km=85, electricity_kwh=35,
             device_hours_per_day=8, paper_sheets=10, food_subtype="mixed_diet")

# domain bounds [a, b] of the four numeric inputs (SRS / schema.sql)
BOUNDS = {"distance_km": (0, 300), "electricity_kwh": (0, 150),
          "device_hours_per_day": (0, 24), "paper_sheets": (0, 100)}
EPS = 0.01
TRANSPORT = ["car_gasoline", "motorcycle", "traditional_jeep", "bicycle_walking"]
FOOD = ["vegan", "vegetarian", "mixed_diet", "meat_heavy"]


def post(**over):
    body = {**VALID, **over}
    body = {k: v for k, v in body.items() if v is not None}     # None => omit field
    return client.post("/api/estimate", json=body)


# =====================================================================
# 1. BOUNDARY VALUE ANALYSIS
#    For domain [a, b]: a-, a, a+, nominal, b-, b, b+
# =====================================================================
def bva_cases():
    for field, (a, b) in BOUNDS.items():
        nominal = (a + b) / 2
        yield (field, a - EPS, False)       # just below a  -> invalid
        yield (field, a, True)              # a             -> valid
        yield (field, a + EPS, True)        # just above a  -> valid
        yield (field, nominal, True)        # nominal       -> valid
        yield (field, b - EPS, True)        # just below b  -> valid
        yield (field, b, True)              # b             -> valid
        yield (field, b + EPS, False)       # just above b  -> invalid


@pytest.mark.parametrize("field,value,is_valid", list(bva_cases()))
def test_bva_numeric_inputs(fake_db, field, value, is_valid):
    r = post(**{field: value})
    if is_valid:
        assert r.status_code == 200, r.text
        assert len(fake_db.inserted) == 1
    else:
        assert r.status_code == 422
        assert field in json.dumps(r.json()["detail"])      # error names the field
        assert fake_db.inserted == []                        # nothing saved


def make_df(n, seed=0):
    rng = np.random.default_rng(seed)
    d = {"record_id": [f"r{i}" for i in range(n)]}
    for c in FEATURE_COLUMNS:
        d[c] = rng.uniform(1, 50, n).round(3)
    return pd.DataFrame(d)


@pytest.mark.parametrize("n,runs", [(4, False), (5, True), (6, True)])
def test_bva_minimum_data_threshold(monkeypatch, n, runs):
    monkeypatch.setattr(mining, "select_data", lambda: make_df(n))
    monkeypatch.setattr(mining, "persist_results", lambda df, s, records_processed: "run-1")
    assert (run_mining_job() == "run-1") is runs


# =====================================================================
# 2. EQUIVALENCE PARTITIONING
#    Numeric categories (distance, electricity, devices, paper):
#      valid:   inside [a, b]
#      invalid: below a | above b | non-numeric text | missing/null
# =====================================================================
def numeric_classes():
    for field, (a, b) in BOUNDS.items():
        yield (field, (a + b) / 2, 200)        # valid class
        yield (field, -50, 422)                # invalid: below range
        yield (field, b * 10, 422)             # invalid: above range
        yield (field, "abc", 422)              # invalid: wrong type
        yield (field, None, 422)               # invalid: missing


@pytest.mark.parametrize("field,value,expected", list(numeric_classes()))
def test_ep_numeric_categories(fake_db, field, value, expected):
    assert post(**{field: value}).status_code == expected


#    Categorical inputs (transport, food):
#      valid:   every known option
#      invalid: unknown option | empty string | wrong letter case
@pytest.mark.parametrize("sub", TRANSPORT)
def test_ep_transport_valid(fake_db, sub):
    assert post(transport_subtype=sub).status_code == 200


@pytest.mark.parametrize("sub", ["rocket", "", "Car_Gasoline"])
def test_ep_transport_invalid(fake_db, sub):
    r = post(transport_subtype=sub)
    assert r.status_code == 404 and fake_db.inserted == []


@pytest.mark.parametrize("sub", FOOD)
def test_ep_food_valid(fake_db, sub):
    assert post(food_subtype=sub).status_code == 200


@pytest.mark.parametrize("sub", ["carnivore", "", "VEGAN"])
def test_ep_food_invalid(fake_db, sub):
    r = post(food_subtype=sub)
    assert r.status_code == 404 and fake_db.inserted == []


# =====================================================================
# 3. INTERFACE TESTING
# =====================================================================
def read_text(*parts):
    p = ROOT.joinpath(*parts)
    if not p.exists():
        pytest.skip(f"{p} not found (frontend/ and database/ must sit next to backend/)")
    return p.read_text(encoding="utf-8").replace("\r", "")


def schema_tables():
    """{table: {column: full_line}} parsed from database/schema.sql"""
    sql = read_text("database", "schema.sql")
    tables = {}
    for name, body in re.findall(r"create table (\w+) \((.*?)\n\);", sql, re.S):
        cols = {}
        for line in body.split("\n"):
            m = re.match(r"\s{4}([a-z_0-9]+)\s+\S", line)
            if m and m.group(1) not in {"unique", "primary", "check", "foreign", "constraint"}:
                cols[m.group(1)] = line
        tables[name] = cols
    return tables


# ---- 3a. Web form  <->  API  <->  database (names, options, bounds) ----
SLIDER_IDS = {"distance_km": "distance", "electricity_kwh": "electricity",
              "device_hours_per_day": "devices", "paper_sheets": "paper"}


@pytest.mark.parametrize("field,select_id,category", [
    ("transport_subtype", "transportMode", "transport"),
    ("food_subtype", "foodPattern", "food"),
])
def test_form_dropdown_values_exist_in_emission_factors(field, select_id, category):
    html = read_text("frontend", "landing.html")
    block = re.search(rf'id="{select_id}">(.*?)</select>', html, re.S).group(1)
    form_values = set(re.findall(r'value="(\w+)"', block))
    seeded = set(re.findall(rf"\('{category}',\s*'(\w+)'", read_text("database", "seed_emission_factors.sql")))
    assert form_values and form_values == seeded


@pytest.mark.parametrize("field", list(BOUNDS))
def test_form_slider_bounds_match_database_and_api(field):
    html = read_text("frontend", "landing.html")
    m = re.search(rf'id="{SLIDER_IDS[field]}"\s+min="([\d.]+)"\s+max="([\d.]+)"', html)
    form = (float(m.group(1)), float(m.group(2)))
    sql = read_text("database", "schema.sql")
    d = re.search(rf"{field} between (\d+) and (\d+)", sql)
    db = (float(d.group(1)), float(d.group(2)))
    assert form == db == tuple(map(float, BOUNDS[field]))


def test_form_sends_exactly_the_fields_the_api_expects():
    js = read_text("frontend", "landing.html")
    sent = set(re.findall(r"(\w+):\s*(?:transportModeEl|getDistance|parseFloat|foodPatternEl)", js))
    assert sent == set(VALID)


# ---- 3b. API -> Supabase (column names and data types) ----
def test_api_insert_matches_activity_records_columns_and_types(fake_db):
    assert post().status_code == 200
    _, row = fake_db.inserted[0]
    payload = {k: v for k, v in row.items() if k != "record_id"}      # record_id is added by the DB
    cols = schema_tables()["activity_records"]
    assert set(payload) <= set(cols)                                  # no unknown column
    assert "total_co2e" not in payload                                # generated column
    required = {c for c, line in cols.items()
                if "not null" in line and "default" not in line and "generated" not in line}
    assert required <= set(payload)                                   # no required column missing
    for k, v in payload.items():
        if k.endswith("_id"):
            assert isinstance(v, str)
        else:
            assert isinstance(v, (int, float)) and not isinstance(v, bool)
    json.dumps(payload)                                               # JSON-serialisable


# ---- 3c. Supabase -> mining process (read side) and mining -> Supabase (write side) ----
class _Result:
    def __init__(self, data): self.data = data


class _Query:
    def __init__(self, rec, name):
        self.rec, self.name, self.payload = rec, name, None
    def select(self, cols="*", **k): self.rec.selects.append((self.name, cols)); return self
    def eq(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self
    def insert(self, payload): self.payload = payload; return self
    def execute(self):
        if self.payload is None:
            return _Result([])
        rows = self.payload if isinstance(self.payload, list) else [self.payload]
        self.rec.inserts.append((self.name, rows))
        return _Result([{"run_id": "run-1", **rows[0]}])


class Recorder:
    def __init__(self): self.selects, self.inserts = [], []
    def table(self, name): return _Query(self, name)


def test_mining_reads_only_existing_columns(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(mining, "supabase", rec)
    mining.select_data()
    table, cols = rec.selects[0]
    selected = [c.strip() for c in cols.split(",")]
    assert table == "activity_records"
    assert set(selected) <= set(schema_tables()["activity_records"])
    assert set(FEATURE_COLUMNS) <= set(selected)


def test_mining_writes_match_schema_columns_and_types(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(mining, "supabase", rec)
    df = preprocess_data(make_df(8))
    mining.mine(transform_data(df), df)
    mining.persist_results(df, 0.5, records_processed=len(df))

    written = {}
    for table, rows in rec.inserts:
        written.setdefault(table, []).extend(rows)
    schema = schema_tables()

    assert set(written) == {"mining_runs", "cluster_results", "anomaly_results", "processing_log"}
    for table, rows in written.items():
        for row in rows:
            assert set(row) <= set(schema[table]), f"{table}: unknown column in {set(row) - set(schema[table])}"
            json.dumps(row)                                   # no numpy types leaking into the payload

    run = written["mining_runs"][0]
    assert type(run["records_processed"]) is int and type(run["k_clusters"]) is int
    assert type(run["silhouette_score"]) is float
    assert len(written["cluster_results"]) == len(written["anomaly_results"]) == 8
    for r in written["cluster_results"]:
        assert r["cluster_label"] in {"low_emission", "moderate", "high_emission"}
        assert type(r["distance_to_centroid"]) is float and r["run_id"] == "run-1"
    for r in written["anomaly_results"]:
        assert type(r["is_anomaly"]) is bool and type(r["anomaly_score"]) is float
        assert (r["explanation"] is None) == (not r["is_anomaly"])
    for r in written["processing_log"]:
        assert r["event_type"] in {"clustering", "anomaly_detection"} and r["status"] == "success"


# ---- 3d. Order of the whole chain: form payload -> API -> saved rows -> mining ----
def test_saved_estimates_are_valid_mining_input(fake_db):
    variations = [
        dict(distance_km=5, electricity_kwh=10, device_hours_per_day=2, paper_sheets=0, food_subtype="vegan",
             transport_subtype="bicycle_walking"),
        dict(distance_km=40, electricity_kwh=60, device_hours_per_day=6, paper_sheets=20, food_subtype="vegetarian"),
        dict(distance_km=85, electricity_kwh=35, device_hours_per_day=8, paper_sheets=10),
        dict(distance_km=150, electricity_kwh=90, device_hours_per_day=12, paper_sheets=50,
             transport_subtype="traditional_jeep", food_subtype="meat_heavy"),
        dict(distance_km=300, electricity_kwh=150, device_hours_per_day=24, paper_sheets=100,
             transport_subtype="traditional_jeep", food_subtype="meat_heavy"),
        dict(distance_km=20, electricity_kwh=25, device_hours_per_day=4, paper_sheets=5,
             transport_subtype="motorcycle"),
    ]
    for v in variations:
        assert post(**v).status_code == 200
    rows = [row for _, row in fake_db.inserted]
    df = pd.DataFrame(rows)
    df["record_id"] = [f"r{i}" for i in range(len(df))]
    df = preprocess_data(df)
    assert len(df) == len(variations)                          # nothing dropped: API output is clean
    mining.mine(transform_data(df), df)
    assert set(df["cluster_label"]) <= {"low_emission", "moderate", "high_emission"}
