"""
CarbonScope mining job.

Structured explicitly around the 5-phase Knowledge Discovery in
Databases (KDD) process, matching the study's own methodology
section (Fayyad et al., 1996 — Selection, Preprocessing,
Transformation, Data Mining, Interpretation/Evaluation):

    1. Selection       -> select_data()
    2. Preprocessing    -> preprocess_data()
    3. Transformation   -> transform_data()
    4. Data Mining       -> mine(): K-Means clustering + Isolation Forest
    5. Interpretation/
       Evaluation        -> evaluate() + persist_results()

Run manually with: python -m app.mining
"""

import uuid
from datetime import datetime, timezone

import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from app.database import supabase

FEATURE_COLUMNS = [
    "transport_co2e",
    "electricity_co2e",
    "devices_co2e",
    "paper_co2e",
    "food_co2e",
]

K_CLUSTERS = 3
CLUSTER_LABEL_ORDER = ["low_emission", "moderate", "high_emission"]
ISOLATION_CONTAMINATION = 0.02
MIN_RECORDS_REQUIRED = 5


def select_data() -> pd.DataFrame:
    """
    Phase 1 — Selection.
    Pulls the accumulated activity_records into a DataFrame. This is
    the "target data set" the rest of the KDD process operates on.
    """
    result = (
        supabase.table("activity_records")
        .select("record_id, " + ", ".join(FEATURE_COLUMNS))
        .execute()
    )
    df = pd.DataFrame(result.data)
    return df


def preprocess_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Phase 2 — Preprocessing.
    Drops incomplete rows and coerces feature columns to numeric,
    consistent with the study's documented handling of missing or
    invalid values before analysis.
    """
    df = df.dropna(subset=FEATURE_COLUMNS).copy()
    for col in FEATURE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=FEATURE_COLUMNS)
    df = df.reset_index(drop=True)
    return df


def transform_data(df: pd.DataFrame):
    """
    Phase 3 — Transformation.
    Standardizes the five CO2e feature columns so no single category
    (e.g. electricity, which tends to have larger raw values) dominates
    the distance calculations used by K-Means and Isolation Forest.
    """
    scaler = StandardScaler()
    scaled = scaler.fit_transform(df[FEATURE_COLUMNS])
    return scaled


def mine(scaled_features, df: pd.DataFrame):
    """
    Phase 4 — Data Mining.
    Applies K-Means Clustering to group records with similar
    activity/emission profiles, and Isolation Forest to flag records
    that are unusual relative to the rest of the dataset.
    """
    kmeans = KMeans(n_clusters=K_CLUSTERS, random_state=42, n_init=10)
    cluster_indices = kmeans.fit_predict(scaled_features)

    centroid_totals = kmeans.cluster_centers_.sum(axis=1)
    ranked_clusters = centroid_totals.argsort()
    index_to_label = {
        cluster_idx: CLUSTER_LABEL_ORDER[rank]
        for rank, cluster_idx in enumerate(ranked_clusters)
    }
    df["cluster_label"] = [index_to_label[i] for i in cluster_indices]

    centroids = kmeans.cluster_centers_
    df["distance_to_centroid"] = [
        ((scaled_features[i] - centroids[cluster_indices[i]]) ** 2).sum() ** 0.5
        for i in range(len(df))
    ]

    iso_forest = IsolationForest(
        contamination=ISOLATION_CONTAMINATION, random_state=42
    )
    predictions = iso_forest.fit_predict(scaled_features)
    scores = iso_forest.decision_function(scaled_features)
    df["is_anomaly"] = predictions == -1
    df["anomaly_score"] = scores

    return kmeans, cluster_indices


def evaluate(scaled_features, cluster_indices) -> float:
    """
    Phase 5a — Evaluation.
    Computes the silhouette score, the metric your methodology
    specifies for assessing K-Means cluster separation quality.
    """
    if len(set(cluster_indices)) < 2:
        return None
    return float(silhouette_score(scaled_features, cluster_indices))


def build_explanation(row: pd.Series) -> str:
    """
    Helper for Phase 5b — Interpretation.
    Produces the plain-language explanation shown on the Anomalies
    page, based on which category contributed most to the record's
    total CO2e.
    """
    if not row["is_anomaly"]:
        return None
    parts = {col: row[col] for col in FEATURE_COLUMNS}
    dominant = max(parts, key=parts.get)
    dominant_label = dominant.replace("_co2e", "")
    return (
        f"Flagged as unusual relative to the rest of the dataset, driven "
        f"largely by {dominant_label} ({parts[dominant]:.1f} kg CO2e)."
    )


def persist_results(df: pd.DataFrame, silhouette: float, records_processed: int):
    """
    Phase 5c — Interpretation/Knowledge Presentation.
    Writes the run metadata, cluster assignments, and anomaly flags
    back to Supabase so the Report and Anomalies pages can read them
    without recomputing anything.
    """
    run_insert = (
        supabase.table("mining_runs")
        .insert(
            {
                "records_processed": records_processed,
                "k_clusters": K_CLUSTERS,
                "silhouette_score": silhouette,
                "isolation_contamination": ISOLATION_CONTAMINATION,
                "triggered_by": "manual",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "completed",
            }
        )
        .execute()
    )
    run_id = run_insert.data[0]["run_id"]

    cluster_rows = [
        {
            "run_id": run_id,
            "record_id": row["record_id"],
            "cluster_label": row["cluster_label"],
            "distance_to_centroid": round(float(row["distance_to_centroid"]), 4),
        }
        for _, row in df.iterrows()
    ]
    supabase.table("cluster_results").insert(cluster_rows).execute()

    anomaly_rows = [
        {
            "run_id": run_id,
            "record_id": row["record_id"],
            "is_anomaly": bool(row["is_anomaly"]),
            "anomaly_score": round(float(row["anomaly_score"]), 4),
            "explanation": build_explanation(row),
        }
        for _, row in df.iterrows()
    ]
    supabase.table("anomaly_results").insert(anomaly_rows).execute()

    supabase.table("processing_log").insert(
        {
            "event_type": "clustering",
            "run_id": run_id,
            "records_processed": records_processed,
            "status": "success",
            "details": {"silhouette_score": silhouette, "k": K_CLUSTERS},
        }
    ).execute()
    supabase.table("processing_log").insert(
        {
            "event_type": "anomaly_detection",
            "run_id": run_id,
            "records_processed": records_processed,
            "status": "success",
            "details": {"contamination": ISOLATION_CONTAMINATION},
        }
    ).execute()

    return run_id


def run_mining_job():
    df = select_data()

    if len(df) < MIN_RECORDS_REQUIRED:
        print(
            f"Only {len(df)} record(s) in activity_records — need at least "
            f"{MIN_RECORDS_REQUIRED} before clustering is meaningful. "
            f"Submit more estimates from the Estimate page first."
        )
        return None

    df = preprocess_data(df)
    scaled_features = transform_data(df)
    kmeans, cluster_indices = mine(scaled_features, df)
    silhouette = evaluate(scaled_features, cluster_indices)
    run_id = persist_results(df, silhouette, records_processed=len(df))

    print(f"Mining run {run_id} complete.")
    print(f"Records processed: {len(df)}")
    print(f"Silhouette score: {silhouette}")
    print(df["cluster_label"].value_counts().to_string())
    print(f"Anomalies flagged: {int(df['is_anomaly'].sum())}")

    return run_id


if __name__ == "__main__":
    run_mining_job()
