from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.estimate import router as estimate_router
from app.report import router as report_router

app = FastAPI(title="CarbonScope API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(estimate_router)
app.include_router(report_router)


@app.get("/")
def health_check():
    return {"status": "ok", "service": "CarbonScope API"}
