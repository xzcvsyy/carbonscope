from fastapi import APIRouter, Query

from app.database import supabase

router = APIRouter()

LOOKUP_CHUNK_SIZE = 100  # keeps the .in_() URL short enough


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


@router.get("/api/anomalies")
def get_anomalies(limit: int = Query(200, ge=1, le=1000)):
    run_result = supabase.table("latest_run_summary").select("*").execute()
    latest_run = run_result.data[0] if run_result.data else None
    if not latest_run:
        return {"has_mining_run": False, "total_flagged": 0, "anomalies": []}

    # Most unusual first: Isolation Forest's decision_function is lowest for
    # the most anomalous records.
    flagged = (
        supabase.table("anomaly_results")
        .select("record_id, anomaly_score, explanation")
        .eq("run_id", latest_run["run_id"])
        .eq("is_anomaly", True)
        .order("anomaly_score")
        .limit(limit)
        .execute()
        .data
        or []
    )

    record_ids = [row["record_id"] for row in flagged]
    records = {}
    for batch in chunks(record_ids, LOOKUP_CHUNK_SIZE):
        rows = (
            supabase.table("activity_records")
            .select("record_id, submitted_at, total_co2e, dataset_id")
            .in_("record_id", batch)
            .execute()
            .data
            or []
        )
        for row in rows:
            records[row["record_id"]] = row

    dataset_rows = supabase.table("datasets").select("dataset_id, name").execute().data or []
    dataset_names = {row["dataset_id"]: row["name"] for row in dataset_rows}

    anomalies = []
    for row in flagged:
        record = records.get(row["record_id"])
        if record is None:  # record was deleted after the run (e.g. dataset re-import)
            continue
        anomalies.append(
            {
                "record_id": row["record_id"],
                "short_id": "REC-" + str(row["record_id"])[:8].upper(),
                "total_co2e": round(float(record["total_co2e"]), 1),
                "explanation": row["explanation"],
                "submitted_at": record["submitted_at"],
                "anomaly_score": round(float(row["anomaly_score"]), 2),
                "source": dataset_names.get(record["dataset_id"], "Live estimate")
                if record["dataset_id"]
                else "Live estimate",
            }
        )

    return {
        "has_mining_run": True,
        "total_flagged": latest_run["anomaly_count"] or len(anomalies),
        "anomalies": anomalies,
    }
