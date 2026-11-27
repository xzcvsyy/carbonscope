"""
Test doubles shared by all unit tests.

app.database creates a real Supabase client at import time and raises if
the .env values are missing. For unit tests we replace that whole module
with a FakeSupabase BEFORE any app module is imported, so no network or
.env file is needed (this is the "stub" in driver/stub testing).
"""
import sys
import types

import pytest


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters = {}
        self.payload = None

    # query-builder methods used by the app code
    def select(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def order(self, *a, **k): return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        if self.payload is not None:                      # INSERT
            if self.db.fail_insert:
                return FakeResult([])
            row = {"record_id": "rec-test-1", **self.payload}
            self.db.inserted.append((self.table, row))
            return FakeResult([row])
        if self.table == "emission_factors":              # factor lookup
            key = (self.filters.get("category"), self.filters.get("subtype"))
            row = self.db.factors.get(key)
            return FakeResult([row] if row else [])
        return FakeResult([])


class FakeSupabase:
    DEFAULT_FACTORS = {
        ("transport", "car_gasoline"):    {"factor_id": "f-car",   "factor_value": 0.12},
        ("transport", "motorcycle"):      {"factor_id": "f-moto",  "factor_value": 0.09},
        ("transport", "traditional_jeep"):{"factor_id": "f-jeep",  "factor_value": 0.15},
        ("transport", "bicycle_walking"): {"factor_id": "f-bike",  "factor_value": 0.0},
        ("electricity", "grid_average"):  {"factor_id": "f-elec",  "factor_value": 0.45},
        ("devices", "general_use"):       {"factor_id": "f-dev",   "factor_value": 0.10},
        ("paper", "general_use"):         {"factor_id": "f-paper", "factor_value": 0.02},
        ("food", "vegan"):                {"factor_id": "f-vegan", "factor_value": 10.0},
        ("food", "vegetarian"):           {"factor_id": "f-veg",   "factor_value": 15.0},
        ("food", "mixed_diet"):           {"factor_id": "f-mixed", "factor_value": 22.0},
        ("food", "meat_heavy"):           {"factor_id": "f-meat",  "factor_value": 35.0},
    }

    def __init__(self):
        self.reset()

    def reset(self):
        self.factors = dict(self.DEFAULT_FACTORS)
        self.inserted = []
        self.fail_insert = False

    def table(self, name):
        return FakeQuery(self, name)


_fake = FakeSupabase()
_stub = types.ModuleType("app.database")
_stub.supabase = _fake
sys.modules["app.database"] = _stub          # must happen before importing app.*


@pytest.fixture
def fake_db():
    _fake.reset()
    return _fake
