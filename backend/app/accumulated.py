from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from app.database import supabase
from app.settings import MIN_RECORDS_REQUIRED

router = APIRouter()

CATEGORY_COLUMNS = {
    "transport": "transport_co2e",
    "electricity": "electricity_co2e",
    "devices": "devices_co2e",
    "paper": "paper_co2e",
    "food": "food_co2e",
}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@router.get("/api/accumulated")
def get_accumulated():
    """
    Accumulated activity information for the Estimate page (SRS Feature 3):
    how many records have been collected, their total CO2e, the last 7 days,
    and progress toward the minimum data requirement (Feature 4).
    """
    result = (
        supabase.table("activity_records")
        .select(
            "record_id, submitted_at, total_co2e, "
            + ", ".join(CATEGORY_COLUMNS.values())
        )
        .execute()
    )
    records = result.data or []

    week_start = datetime.now(timezone.utc) - timedelta(days=7)
    total = 0.0
    last_7_days = 0.0
    by_category = {name: 0.0 for name in CATEGORY_COLUMNS}
    for r in records:
        value = float(r["total_co2e"])
        total += value
        if _parse(r["submitted_at"]) >= week_start:
            last_7_days += value
        for name, column in CATEGORY_COLUMNS.items():
            by_category[name] += float(r[column])

    count = len(records)
    return {
        "records_count": count,
        "total_co2e": round(total, 1),
        "last_7_days_co2e": round(last_7_days, 1),
        "by_category": {k: round(v, 1) for k, v in by_category.items()},
        "minimum_required": MIN_RECORDS_REQUIRED,
        "meets_minimum": count >= MIN_RECORDS_REQUIRED,
        "records_remaining": max(0, MIN_RECORDS_REQUIRED - count),
    }
