"""
Unit tests for the mining job (app/mining.py).
  - Minimum-data check (n >= 5): condition testing, true and false sides
  - Preprocessing / Z-score standardization: loop testing on n
      (skip=0, one pass=1, two passes=2, n-1, n, n+1) + malformed entries
  - Error-handling: skipped run, clear message
Supabase is stubbed (see conftest.py); select_data/persist_results are
monkeypatched so only the logic under test runs.
"""
import numpy as np
import pandas as pd
import pytest

from app import mining
from app.mining import (FEATURE_COLUMNS, MIN_RECORDS_REQUIRED, build_explanation,
                        evaluate, preprocess_data, run_mining_job, transform_data)

N_TYPICAL = 10   # the "n" in the loop-test sizes below


def make_df(n, seed=0):
    rng = np.random.default_rng(seed)
    data = {"record_id": [f"r{i}" for i in range(n)]}
    for c in FEATURE_COLUMNS:
        data[c] = rng.uniform(1, 50, n).round(3)
    return pd.DataFrame(data)


@pytest.fixture
def spy(monkeypatch):
    """Stub DB read/write; record whether persist_results was reached."""
    calls = {"persist": 0}

    def fake_persist(df, silhouette, records_processed):
        calls["persist"] += 1
        return "run-1"

    monkeypatch.setattr(mining, "persist_results", fake_persist)
    return calls


# ---------------- Minimum data check: condition testing ----------------
@pytest.mark.parametrize("n", [0, 1, MIN_RECORDS_REQUIRED - 1])      # FALSE side
def test_below_minimum_skips_run(monkeypatch, spy, n):
    monkeypatch.setattr(mining, "select_data", lambda: make_df(n))
    assert run_mining_job() is None
    assert spy["persist"] == 0


@pytest.mark.parametrize("n", [MIN_RECORDS_REQUIRED, MIN_RECORDS_REQUIRED + 1])  # TRUE side
def test_at_or_above_minimum_runs(monkeypatch, spy, n):
    monkeypatch.setattr(mining, "select_data", lambda: make_df(n))
    assert run_mining_job() == "run-1"
    assert spy["persist"] == 1


def test_skip_message_is_understandable(monkeypatch, spy, capsys):
    monkeypatch.setattr(mining, "select_data", lambda: make_df(2))
    run_mining_job()
    out = capsys.readouterr().out
    assert "2" in out                       # how many records exist
    assert str(MIN_RECORDS_REQUIRED) in out  # how many are needed
    assert "Estimate page" in out            # what the user should do


def test_empty_table_does_not_crash(monkeypatch, spy):
    # supabase returns [] -> DataFrame with no columns at all
    monkeypatch.setattr(mining, "select_data", lambda: pd.DataFrame([]))
    assert run_mining_job() is None


@pytest.mark.xfail(reason="DEFECT: the n>=5 check runs BEFORE preprocessing; rows "
                          "dropped as malformed can leave < k=3 rows and KMeans raises "
                          "ValueError instead of skipping the run gracefully.")
def test_min_check_should_apply_after_preprocessing(monkeypatch, spy):
    df = make_df(5)
    df.loc[0:2, "transport_co2e"] = None     # 3 malformed -> 2 valid rows remain
    monkeypatch.setattr(mining, "select_data", lambda: df)
    assert run_mining_job() is None


# ---------------- Preprocessing: loop testing + malformed entries ----------------
@pytest.mark.parametrize("n", [0, 1, 2, N_TYPICAL - 1, N_TYPICAL, N_TYPICAL + 1])
def test_preprocess_keeps_all_valid_rows_for_loop_sizes(n):
    out = preprocess_data(make_df(n))
    assert len(out) == n
    assert list(out.index) == list(range(n))        # index was reset


def test_preprocess_drops_missing_values():
    df = make_df(5)
    df.loc[1, "food_co2e"] = None
    df.loc[3, "paper_co2e"] = np.nan
    out = preprocess_data(df)
    assert len(out) == 3
    assert set(out["record_id"]) == {"r0", "r2", "r4"}


def test_preprocess_drops_non_numeric_and_coerces_numeric_strings():
    df = make_df(4).astype({c: object for c in FEATURE_COLUMNS})
    df.loc[0, "devices_co2e"] = "abc"      # malformed -> dropped
    df.loc[1, "devices_co2e"] = "3.5"      # numeric string -> kept as 3.5
    out = preprocess_data(df)
    assert "r0" not in set(out["record_id"])
    assert len(out) == 3
    assert out.loc[out["record_id"] == "r1", "devices_co2e"].iloc[0] == 3.5
    assert all(pd.api.types.is_numeric_dtype(out[c]) for c in FEATURE_COLUMNS)


def test_preprocess_all_rows_malformed_returns_empty():
    df = make_df(3)
    df["electricity_co2e"] = None
    assert len(preprocess_data(df)) == 0


# ---------------- Z-score standardization: loop testing ----------------
@pytest.mark.parametrize("n", [2, N_TYPICAL - 1, N_TYPICAL, N_TYPICAL + 1])
def test_zscore_mean0_std1_matches_manual_formula(n):
    df = make_df(n)
    z = transform_data(df)
    assert z.shape == (n, len(FEATURE_COLUMNS))
    np.testing.assert_allclose(z.mean(axis=0), 0, atol=1e-9)
    np.testing.assert_allclose(z.std(axis=0), 1, atol=1e-9)       # population std (ddof=0)
    manual = (df[FEATURE_COLUMNS] - df[FEATURE_COLUMNS].mean()) / df[FEATURE_COLUMNS].std(ddof=0)
    np.testing.assert_allclose(z, manual.to_numpy(), atol=1e-9)


def test_zscore_single_record_gives_zeros_not_nan():
    z = transform_data(make_df(1))
    assert not np.isnan(z).any() and (z == 0).all()


def test_zscore_constant_column_gives_zeros_not_nan():
    df = make_df(6)
    df["food_co2e"] = 22.0                  # zero variance column
    z = transform_data(df)
    assert not np.isnan(z).any()
    assert (z[:, FEATURE_COLUMNS.index("food_co2e")] == 0).all()


def test_zscore_zero_records_raises_clear_error():
    with pytest.raises(ValueError):          # skip-loop case: scaler refuses 0 rows
        transform_data(make_df(0))


# ---------------- Evaluation + interpretation ----------------
def test_evaluate_single_cluster_returns_none():
    assert evaluate(np.random.rand(6, 5), np.zeros(6, dtype=int)) is None


def test_evaluate_two_clusters_returns_valid_silhouette():
    x = np.vstack([np.zeros((3, 2)), np.ones((3, 2)) * 5]) + np.random.rand(6, 2) * 0.1
    s = evaluate(x, np.array([0, 0, 0, 1, 1, 1]))
    assert -1 <= s <= 1 and s > 0.9


def test_build_explanation_non_anomaly_is_none():
    row = make_df(1).iloc[0].copy()
    row["is_anomaly"] = False
    assert build_explanation(row) is None


def test_build_explanation_names_dominant_category():
    row = pd.Series({"transport_co2e": 1.0, "electricity_co2e": 99.5, "devices_co2e": 0.8,
                     "paper_co2e": 0.2, "food_co2e": 22.0, "is_anomaly": True})
    text = build_explanation(row)
    assert "electricity" in text and "99.5" in text


# ---------------- Full pipeline: outputs are well-formed ----------------
def test_mine_assigns_labels_and_flags_for_every_record():
    df = preprocess_data(make_df(30, seed=3))
    scaled = transform_data(df)
    mining.mine(scaled, df)
    assert set(df["cluster_label"]) <= set(mining.CLUSTER_LABEL_ORDER)
    assert df["is_anomaly"].dtype == bool
    assert (df["distance_to_centroid"] >= 0).all()
    assert len(df) == 30
