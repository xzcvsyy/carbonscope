from fastapi import APIRouter, HTTPException
from app.database import supabase
from app.schemas import EstimateRequest, EstimateResponse, EstimateBreakdown

router = APIRouter()


def get_factor(category: str, subtype: str) -> dict:
    result = (
        supabase.table("emission_factors")
        .select("factor_id, factor_value")
        .eq("category", category)
        .eq("subtype", subtype)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"No active emission factor found for {category}/{subtype}",
        )
    return result.data[0]


def classify_interim(total_co2e: float) -> str:
    """
    Threshold-based placeholder classification used until the K-Means
    mining job has run at least once and produced real cluster
    assignments in cluster_results. Replace calls to this with a
    lookup against the latest mining_runs / cluster_results once
    that job exists.
    """
    if total_co2e < 20:
        return "low_emission"
    if total_co2e < 45:
        return "moderate"
    return "high_emission"


@router.post("/api/estimate", response_model=EstimateResponse)
def estimate(payload: EstimateRequest):
    transport_factor = get_factor("transport", payload.transport_subtype)
    electricity_factor = get_factor("electricity", "grid_average")
    devices_factor = get_factor("devices", "general_use")
    paper_factor = get_factor("paper", "general_use")
    food_factor = get_factor("food", payload.food_subtype)

    transport_co2e = payload.distance_km * transport_factor["factor_value"]
    electricity_co2e = payload.electricity_kwh * electricity_factor["factor_value"]
    devices_co2e = payload.device_hours_per_day * devices_factor["factor_value"]
    paper_co2e = payload.paper_sheets * paper_factor["factor_value"]
    food_co2e = food_factor["factor_value"]  # already a weekly total, not multiplied

    total_co2e = (
        transport_co2e + electricity_co2e + devices_co2e + paper_co2e + food_co2e
    )

    insert_result = (
        supabase.table("activity_records")
        .insert(
            {
                "transport_factor_id": transport_factor["factor_id"],
                "food_factor_id": food_factor["factor_id"],
                "distance_km": payload.distance_km,
                "electricity_kwh": payload.electricity_kwh,
                "device_hours_per_day": payload.device_hours_per_day,
                "paper_sheets": payload.paper_sheets,
                "transport_co2e": round(transport_co2e, 3),
                "electricity_co2e": round(electricity_co2e, 3),
                "devices_co2e": round(devices_co2e, 3),
                "paper_co2e": round(paper_co2e, 3),
                "food_co2e": round(food_co2e, 3),
            }
        )
        .execute()
    )

    if not insert_result.data:
        raise HTTPException(status_code=500, detail="Failed to save activity record")

    record = insert_result.data[0]

    return EstimateResponse(
        record_id=record["record_id"],
        breakdown=EstimateBreakdown(
            transport_co2e=round(transport_co2e, 3),
            electricity_co2e=round(electricity_co2e, 3),
            devices_co2e=round(devices_co2e, 3),
            paper_co2e=round(paper_co2e, 3),
            food_co2e=round(food_co2e, 3),
            total_co2e=round(total_co2e, 3),
        ),
        nearest_cluster=classify_interim(total_co2e),
        note=(
            "Cluster is an interim threshold-based estimate. It will be "
            "replaced by real K-Means results once the mining job has run."
        ),
    )
