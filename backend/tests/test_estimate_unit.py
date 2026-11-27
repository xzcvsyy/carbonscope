"""
Unit tests for Process 1.0 — CO2e computation (app/estimate.py).
White-box: basis path + boundary tests.

Cyclomatic complexity V(G) = P + 1 (P = decision points), counted by hand
(verify with:  radon cc app/estimate.py -s):

    get_factor()        1 decision  (if not result.data)        V = 2
    classify_interim()  2 decisions (if <20, if <45)            V = 3
    estimate()          1 decision  (if not insert_result.data) V = 2
                        (+ 5 calls to get_factor, each can raise 404)

=> minimum independent paths to cover = 2 + 3 + 2 = 7 for validation of
   the lookup/classify/save logic, plus per-category 404 paths below.
"""
import pytest
from fastapi import HTTPException

from app.estimate import classify_interim, estimate, get_factor
from app.schemas import EstimateRequest


def make_req(**over):
    base = dict(
        transport_subtype="car_gasoline", distance_km=85,
        electricity_kwh=35, device_hours_per_day=8,
        paper_sheets=10, food_subtype="mixed_diet",
    )
    base.update(over)
    return EstimateRequest(**base)


# ---------- get_factor: both branches (V=2) ----------
def test_get_factor_found(fake_db):
    f = get_factor("transport", "car_gasoline")
    assert f == {"factor_id": "f-car", "factor_value": 0.12}


def test_get_factor_missing_raises_404_with_helpful_message(fake_db):
    with pytest.raises(HTTPException) as e:
        get_factor("transport", "helicopter")
    assert e.value.status_code == 404
    # error message must point to the cause (category AND subtype)
    assert "transport" in e.value.detail and "helicopter" in e.value.detail


# ---------- classify_interim: 3 paths + boundaries (V=3) ----------
@pytest.mark.parametrize("total,expected", [
    (0, "low_emission"),
    (19.999, "low_emission"),
    (20, "moderate"),          # boundary: first value of 'moderate'
    (44.999, "moderate"),
    (45, "high_emission"),     # boundary: first value of 'high_emission'
    (500, "high_emission"),
])
def test_classify_interim(total, expected):
    assert classify_interim(total) == expected


# ---------- estimate(): path 1 — happy path, per-category math ----------
def test_estimate_happy_path_per_category_and_total(fake_db):
    res = estimate(make_req())
    b = res.breakdown
    assert b.transport_co2e == pytest.approx(85 * 0.12)      # 10.2
    assert b.electricity_co2e == pytest.approx(35 * 0.45)    # 15.75
    assert b.devices_co2e == pytest.approx(8 * 0.10)         # 0.8
    assert b.paper_co2e == pytest.approx(10 * 0.02)          # 0.2
    assert b.food_co2e == pytest.approx(22.0)                # flat weekly, NOT multiplied
    assert b.total_co2e == pytest.approx(48.95)
    assert res.nearest_cluster == "high_emission"            # 48.95 >= 45
    assert res.record_id == "rec-test-1"


def test_estimate_saves_row_with_rounded_values_and_factor_ids(fake_db):
    estimate(make_req())
    table, row = fake_db.inserted[0]
    assert table == "activity_records"
    assert row["transport_factor_id"] == "f-car"
    assert row["food_factor_id"] == "f-mixed"
    assert row["electricity_co2e"] == 15.75
    assert "total_co2e" not in row          # generated column: DB computes it


def test_estimate_all_zero_inputs_only_food_remains(fake_db):
    res = estimate(make_req(transport_subtype="bicycle_walking", distance_km=0,
                            electricity_kwh=0, device_hours_per_day=0,
                            paper_sheets=0, food_subtype="vegan"))
    assert res.breakdown.total_co2e == pytest.approx(10.0)
    assert res.nearest_cluster == "low_emission"


def test_estimate_max_bounds(fake_db):
    res = estimate(make_req(transport_subtype="traditional_jeep", distance_km=300,
                            electricity_kwh=150, device_hours_per_day=24,
                            paper_sheets=100, food_subtype="meat_heavy"))
    expected = 300*0.15 + 150*0.45 + 24*0.10 + 100*0.02 + 35
    assert res.breakdown.total_co2e == pytest.approx(expected)


# ---------- estimate(): path 2 — save fails -> 500 ----------
def test_estimate_insert_failure_returns_500(fake_db):
    fake_db.fail_insert = True
    with pytest.raises(HTTPException) as e:
        estimate(make_req())
    assert e.value.status_code == 500
    assert "save" in e.value.detail.lower()


# ---------- per-category validation: each lookup can fail -> 404 ----------
@pytest.mark.parametrize("missing_key", [
    ("transport", "car_gasoline"),
    ("electricity", "grid_average"),
    ("devices", "general_use"),
    ("paper", "general_use"),
    ("food", "mixed_diet"),
])
def test_estimate_missing_factor_for_each_category(fake_db, missing_key):
    del fake_db.factors[missing_key]
    with pytest.raises(HTTPException) as e:
        estimate(make_req())
    assert e.value.status_code == 404
    assert missing_key[0] in e.value.detail
    assert fake_db.inserted == []           # nothing saved on failure


def test_estimate_unknown_dropdown_value(fake_db):
    with pytest.raises(HTTPException) as e:
        estimate(make_req(food_subtype="carnivore"))
    assert e.value.status_code == 404 and "carnivore" in e.value.detail
