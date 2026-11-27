"""
Error-handling paths at the API boundary (pydantic schema, HTTP layer).
Invalid input must be rejected with 422 BEFORE any database call, and the
error body must name the offending field.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID = dict(transport_subtype="car_gasoline", distance_km=85, electricity_kwh=35,
             device_hours_per_day=8, paper_sheets=10, food_subtype="mixed_diet")


def post(**over):
    body = {**VALID, **over}
    body = {k: v for k, v in body.items() if v is not None}   # None => omit field
    return client.post("/api/estimate", json=body)


def fields_in_error(resp):
    return [".".join(map(str, d["loc"])) for d in resp.json()["detail"]]


@pytest.mark.parametrize("field,bad", [
    ("distance_km", -1), ("distance_km", 301),
    ("electricity_kwh", -0.1), ("electricity_kwh", 151),
    ("device_hours_per_day", 24.5),
    ("paper_sheets", 101),
])
def test_out_of_range_rejected_and_field_named(fake_db, field, bad):
    r = post(**{field: bad})
    assert r.status_code == 422
    assert any(field in f for f in fields_in_error(r))
    assert fake_db.inserted == []


@pytest.mark.parametrize("field", list(VALID))
def test_missing_field_rejected(fake_db, field):
    r = post(**{field: None})
    assert r.status_code == 422
    assert any(field in f for f in fields_in_error(r))


def test_wrong_type_rejected(fake_db):
    assert post(distance_km="far").status_code == 422


@pytest.mark.parametrize("field,edge", [
    ("distance_km", 0), ("distance_km", 300),
    ("electricity_kwh", 0), ("electricity_kwh", 150),
    ("device_hours_per_day", 24), ("paper_sheets", 100),
])
def test_boundary_values_accepted(fake_db, field, edge):
    assert post(**{field: edge}).status_code == 200


def test_unknown_subtype_gives_404_with_cause(fake_db):
    r = post(transport_subtype="rocket")
    assert r.status_code == 404
    assert "rocket" in r.json()["detail"]
