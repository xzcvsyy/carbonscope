from pydantic import BaseModel, Field


class EstimateRequest(BaseModel):
    transport_subtype: str = Field(..., examples=["car_gasoline"])
    distance_km: float = Field(..., ge=0, le=300)
    electricity_kwh: float = Field(..., ge=0, le=150)
    device_hours_per_day: float = Field(..., ge=0, le=24)
    paper_sheets: float = Field(..., ge=0, le=100)
    food_subtype: str = Field(..., examples=["mixed_diet"])


class EstimateBreakdown(BaseModel):
    transport_co2e: float
    electricity_co2e: float
    devices_co2e: float
    paper_co2e: float
    food_co2e: float
    total_co2e: float


class EstimateResponse(BaseModel):
    record_id: str
    breakdown: EstimateBreakdown
    nearest_cluster: str
    note: str
