from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter

from app.database import supabase

router = APIRouter()


@router.get("/api/report")
def get_report():
    run_result = supabase.table("latest_run_summary").select("*").execute()
    latest_run = run_result.data[0] if run_result.data else None

    dataset_result = (
        supabase.table("datasets")
        .select("name, source")
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if dataset_result.data:
        row = dataset_result.data[0]
        dataset_source = row["name"] or row["source"]
    else:
        dataset_source = "Live activity records (Estimate page submissions)"

    records_result = (
        supabase.table("activity_records")
        .select("record_id, submitted_at, total_co2e")
        .order("submitted_at")
        .execute()
    )
    records = records_result.data or []

    anomaly_record_ids = set()
    if latest_run:
        anomaly_result = (
            supabase.table("anomaly_results")
            .select("record_id")
            .eq("run_id", latest_run["run_id"])
            .eq("is_anomaly", True)
            .execute()
        )
        anomaly_record_ids = {row["record_id"] for row in (anomaly_result.data or [])}

    daily_totals = defaultdict(float)
    daily_anomalous = defaultdict(bool)
    for r in records:
        date_key = r["submitted_at"][:10]
        daily_totals[date_key] += float(r["total_co2e"])
        if r["record_id"] in anomaly_record_ids:
            daily_anomalous[date_key] = True

    recent_dates = sorted(daily_totals.keys())[-8:]
    weekly_trend = []
    for date_key in recent_dates:
        dt = datetime.strptime(date_key, "%Y-%m-%d")
        weekly_trend.append(
            {
                "label": dt.strftime("%b %d"),
                "total_co2e": round(daily_totals[date_key], 1),
                "is_anomalous": daily_anomalous[date_key],
            }
        )

    if latest_run:
        cluster_distribution = {
            "low_emission": latest_run["low_emission_count"] or 0,
            "moderate": latest_run["moderate_count"] or 0,
            "high_emission": latest_run["high_emission_count"] or 0,
        }
        anomalies_flagged = latest_run["anomaly_count"] or 0
        silhouette_score = latest_run["silhouette_score"]
        isolation_contamination = latest_run["isolation_contamination"]
        records_processed = latest_run["records_processed"]
        total_co2e_tonnes = latest_run["total_co2e_tonnes"] or 0
    else:
        cluster_distribution = {"low_emission": 0, "moderate": 0, "high_emission": 0}
        anomalies_flagged = 0
        silhouette_score = None
        isolation_contamination = None
        records_processed = len(records)
        total_co2e_tonnes = (
            round(sum(float(r["total_co2e"]) for r in records) / 1000, 2)
            if records
            else 0
        )

    return {
        "has_mining_run": latest_run is not None,
        "total_co2e_tonnes": total_co2e_tonnes,
        "records_processed": records_processed,
        "anomalies_flagged": anomalies_flagged,
        "cluster_distribution": cluster_distribution,
        "silhouette_score": silhouette_score,
        "isolation_contamination": isolation_contamination,
        "dataset_source": dataset_source,
        "weekly_trend": weekly_trend,
    }
