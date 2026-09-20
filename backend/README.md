# CarbonScope backend

Minimal FastAPI service that connects to the Supabase schema you already
set up. Right now it exposes one endpoint, `POST /api/estimate`, which
looks up emission factors, computes a CO2e breakdown, saves the record,
and returns an interim (threshold-based) cluster label until the K-Means
mining job exists.

## Setup

```bash
cd carbonscope-backend
python -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in your Supabase project's URL and **service_role**
key (Project Settings > API in the Supabase dashboard). Never commit this
file or use it in frontend code.

## Run it

```bash
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000/docs` — FastAPI's built-in interactive docs,
where you can try the endpoint directly in the browser.

## Test the estimate endpoint

```bash
curl -X POST http://127.0.0.1:8000/api/estimate \
  -H "Content-Type: application/json" \
  -d '{
    "transport_subtype": "car_gasoline",
    "distance_km": 85,
    "electricity_kwh": 35,
    "device_hours_per_day": 8,
    "paper_sheets": 10,
    "food_subtype": "mixed_diet"
  }'
```

Valid `transport_subtype` values: `car_gasoline`, `motorcycle`,
`traditional_jeep`, `bicycle_walking`.

Valid `food_subtype` values: `vegan`, `vegetarian`, `mixed_diet`,
`meat_heavy`.

A successful call returns the CO2e breakdown, the new `record_id`, and
an interim cluster label — and you'll see a new row appear in
`activity_records` in Supabase's Table Editor.

## Next steps

- Add a `GET /api/emission-factors` endpoint so the frontend can load
  dropdown options from the database instead of hardcoding them.
- Add the K-Means + Isolation Forest mining job (reads all of
  `activity_records`, writes to `mining_runs`, `cluster_results`,
  `anomaly_results`).
- Add `GET /api/anomalies` and `GET /api/report` endpoints for those
  frontend pages.
- Deploy to Render, point `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` at
  the same project, and update the frontend's fetch calls to the
  deployed URL.
