from fastapi import APIRouter

from app.database import supabase

router = APIRouter()


@router.get("/api/anomalies")
def get_anomalies():
    """
    Returns the records Isolation Forest flagged in the latest completed
    mining run, most anomalous first (lowest anomaly_score).
    """
    run_result = supabase.table("latest_run_summary").select("*").execute()
    latest_run = run_result.data[0] if run_result.data else None

    if not latest_run:
        return {"has_mining_run": False, "anomalies": []}

    anomaly_result = (
        supabase.table("anomaly_results")
        .select("record_id, anomaly_score, explanation")
        .eq("run_id", latest_run["run_id"])
        .eq("is_anomaly", True)
        .order("anomaly_score")
        .execute()
    )
    flagged = anomaly_result.data or []

    record_ids = [row["record_id"] for row in flagged]
    records_by_id = {}
    if record_ids:
        records_result = (
            supabase.table("activity_records")
            .select("record_id, submitted_at, total_co2e")
            .in_("record_id", record_ids)
            .execute()
        )
        records_by_id = {r["record_id"]: r for r in (records_result.data or [])}

    anomalies = []
    for row in flagged:
        record = records_by_id.get(row["record_id"])
        if record is None:
            continue
        anomalies.append(
            {
                "record_id": row["record_id"],
                "total_co2e": round(float(record["total_co2e"]), 1),
                "explanation": row["explanation"]
                or "Flagged as unusual relative to the rest of the dataset.",
                "submitted_at": record["submitted_at"],
                "anomaly_score": float(row["anomaly_score"]),
            }
        )

    return {"has_mining_run": True, "anomalies": anomalies}
