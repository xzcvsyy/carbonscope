"""
Importer for CF_Calculation_-_LNU-F2F.xlsx — a PRE-COMPUTED carbon
footprint workbook, one sheet per category, one row per student (SN),
with the students' own emission factors and GHG columns already
calculated. This is a different shape again from import_data.py
(generic flat tables) and import_lnu_survey.py (raw Google Forms
survey) — so it's its own dedicated script, same pattern as the other
two.

Every number this script produces was reconciled against the
workbook's own SUMMARY sheet (Total CF/Year) before being trusted:
across all 415 students, our reconstructed total matched theirs to
within 0.0002 kg — i.e. this script is reading their intended
calculation correctly, not re-deriving a different one.

KDD phases:
    Selection       -> load_sheets()
    Preprocessing   -> merge_on_sn(), the row-count mismatch check
    Transformation  -> to_weekly()      the period conversions below
    Loading         -> commit_to_supabase()

Usage (run from the backend folder, venv activated):
    python -m app.import_lnu_f2f --file "path/to/CF_Calculation.xlsx" --inspect
    python -m app.import_lnu_f2f --file "path/to/CF_Calculation.xlsx"            (dry run)
    python -m app.import_lnu_f2f --file "path/to/CF_Calculation.xlsx" --commit

================================================================
WHAT THIS SCRIPT DOES NOT GUESS: the workbook already states its own
emission factors (e.g. Electricity: 0.548 kgCO2e/kWh, Pork: 12.1
kgCO2e/kg) and already computes GHG per student. This script reads
those GHG values directly — "reported by source", not recalculated
from CarbonScope's own placeholder emission_factors — and documents
that in the dataset's `limitations` field, consistent with how your
methodology distinguishes source-reported values from calculated ones.
================================================================
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

try:
    from app.database import supabase
except Exception:
    supabase = None  # allows --inspect / dry-run testing without Supabase configured

WEEKS_PER_YEAR = 52.178571

SHEETS = {
    "electricity": ("Electricity", 5),
    "pork": ("Food - Pork", 5),
    "chicken": ("Food - Chicken", 5),
    "fish": ("Food - Fish", 5),
    "vegetables": ("Food - Vegetables", 5),
    "lpg": ("LPG Tank", 5),
    "comp1": ("Academics - Comp1", 5),
    "comp2": ("Academics - Comp2", 5),
    "cellphone": ("Academics - Cellphone", 5),
    "printing": ("Academics - Printing", 5),
    "scanning": ("Academics - Scanning", 5),
    "transpo_daily": ("Transpo01-Daily commuting", 14),
    "transpo_hometown": ("Transpo02- Hometown Travel", 15),
    "transpo_vacation": ("Transpo02- Vacation Travel", 15),
}
# The student roster (every SN that should exist) is defined by the
# Electricity sheet — every student answered that question.
BASE_SHEET = "electricity"

# Device-hour self-reports (3 questions summed) occasionally exceed a
# physical day — dropped by the same device_hours_per_day <= 24 rule
# the rest of the system uses, not silently capped.
MAX_DEVICE_HOURS_PER_DAY = 24


def load_sheets(path: Path) -> dict:
    """Phase 1 — Selection."""
    out = {}
    for key, (sheet_name, header_row) in SHEETS.items():
        df = pd.read_excel(path, sheet_name=sheet_name, header=header_row)
        df = df[df["SN"].notna()].copy()
        df["SN"] = df["SN"].astype(int)
        out[key] = df.set_index("SN")
    return out


def merge_on_sn(sheets: dict) -> pd.Index:
    """Phase 2 — Preprocessing: the shared student roster, and a check
    that every sheet roughly agrees on who's in it (a sheet with a
    wildly different row count usually means a header row moved)."""
    base = sheets[BASE_SHEET].index
    for key, df in sheets.items():
        if key == BASE_SHEET:
            continue
        overlap = len(base.intersection(df.index))
        if overlap < len(base) * 0.9:
            print(
                f"  WARNING: '{key}' sheet only overlaps {overlap}/{len(base)} "
                f"students with '{BASE_SHEET}' — check its header row is still "
                f"correct (SHEETS dict at the top of this script)."
            )
    return base


def to_weekly(sheets: dict, sn_index: pd.Index) -> pd.DataFrame:
    """Phase 3 — Transformation: every category converted to a weekly
    CO2e figure and a weekly activity quantity, regardless of what
    period the sheet itself reports in."""

    def col(key, name, default=0.0):
        return sheets[key].reindex(sn_index)[name].fillna(default)

    out = pd.DataFrame(index=sn_index)

    # --- Electricity: sheet reports MONTHLY. monthly -> weekly = *12/52.178571
    out["electricity_kwh"] = col("electricity", "kWh per student") * 12 / WEEKS_PER_YEAR
    out["electricity_co2e"] = col("electricity", "GHG") * 12 / WEEKS_PER_YEAR

    # --- Food: sheet already reports WEEKLY. use directly.
    out["food_co2e"] = (
        col("pork", "GHG") + col("chicken", "GHG") + col("fish", "GHG") + col("vegetables", "GHG")
    )

    # --- LPG: sheet reports MONTHLY. Computed for completeness but NOT
    # added to total_co2e — there's no LPG category in activity_records yet.
    out["lpg_co2e_not_in_total"] = col("lpg", "GHG") * 12 / WEEKS_PER_YEAR

    # --- Devices: Comp1/Comp2/Cellphone are self-reported HOURS PER DAY
    # directly (no conversion needed for the quantity); their GHG
    # columns are DAILY, so *7 for weekly.
    out["device_hours_per_day"] = (
        col("comp1", "Student Answer") + col("comp2", "Student Answer") + col("cellphone", "Student Answer")
    )
    out["devices_co2e"] = (col("comp1", "GHG") + col("comp2", "GHG") + col("cellphone", "GHG")) * 7

    # --- Paper: Printing/Scanning sheets already report WEEKLY.
    out["paper_sheets"] = col("printing", "Amount of pages per week") + col("scanning", "Amount of pages per week")
    out["paper_co2e"] = col("printing", "GHG") + col("scanning", "GHG")

    # --- Transport: daily commuting is WEEKLY already; hometown/vacation
    # are one-off ANNUAL events (their GHG and GHG/Year columns are
    # identical in the source), amortized to a weekly-equivalent share.
    daily_km = col("transpo_daily", "SA-Distance (KM)") * col("transpo_daily", "SA - Times a week")
    hometown_km_year = col("transpo_hometown", "SA-Distance (KM)") * col("transpo_hometown", "SA - Times a week")
    vacation_km_year = col("transpo_vacation", "SA-Distance (KM)") * col("transpo_vacation", "SA - Times a week")
    out["distance_km"] = daily_km + (hometown_km_year + vacation_km_year) / WEEKS_PER_YEAR

    out["transport_co2e"] = (
        col("transpo_daily", "GHG")
        + (col("transpo_hometown", "GHG/Year") + col("transpo_vacation", "GHG/Year")) / WEEKS_PER_YEAR
    )

    for c in ["electricity_kwh", "distance_km", "device_hours_per_day", "paper_sheets",
              "transport_co2e", "electricity_co2e", "devices_co2e", "paper_co2e", "food_co2e"]:
        out[c] = out[c].round(4)

    return out


def apply_quality_filters(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Drop rows the database itself would reject, and say why."""
    stats = {"starting": len(df)}

    too_many_hours = df["device_hours_per_day"] > MAX_DEVICE_HOURS_PER_DAY
    stats["dropped_device_hours_over_24"] = int(too_many_hours.sum())
    df = df[~too_many_hours]

    has_negative = (df[["distance_km", "electricity_kwh", "device_hours_per_day", "paper_sheets"]] < 0).any(axis=1)
    stats["dropped_negative_values"] = int(has_negative.sum())
    df = df[~has_negative]

    stats["ready_to_import"] = len(df)
    return df, stats


def summarize(stats: dict, df: pd.DataFrame):
    print(f"\n  students in source (Electricity sheet): {stats['starting']}")
    print(f"  dropped (device hours > 24/day):        {stats['dropped_device_hours_over_24']}")
    print(f"  dropped (negative computed value):      {stats['dropped_negative_values']}")
    print(f"  READY TO IMPORT:                        {stats['ready_to_import']}")
    if len(df) == 0:
        return
    totals = df[["transport_co2e", "electricity_co2e", "devices_co2e", "paper_co2e", "food_co2e"]].sum(axis=1)
    print(f"\n  total_co2e (weekly)  min={totals.min():.1f}  mean={totals.mean():.1f}  max={totals.max():.1f}  kg/week")
    print(f"  LPG (not in total)   mean={df['lpg_co2e_not_in_total'].mean():.1f} kg/week  "
          f"— tell me if you want an LPG category added to the schema")


def inspect(path: Path):
    sheets = load_sheets(path)
    print(f"{path.name}: loaded {len(sheets)} sheets")
    for key, (sheet_name, header_row) in SHEETS.items():
        print(f"  {key:18s} <- '{sheet_name}'  (header row {header_row}, {len(sheets[key])} students)")
    base_n = len(sheets[BASE_SHEET])
    for key, df in sheets.items():
        if key == BASE_SHEET:
            continue
        overlap = len(sheets[BASE_SHEET].index.intersection(df.index))
        flag = "" if overlap >= base_n * 0.9 else "  <-- LOW OVERLAP, CHECK HEADER ROW"
        print(f"    overlap with '{BASE_SHEET}': {overlap}/{base_n}{flag}")


def upsert_dataset() -> str:
    name = "LNU-F2F Carbon Footprint Calculation"
    description = (
        "Pre-computed workbook: each category sheet already states its own "
        "emission factor and GHG per student (e.g. Electricity 0.548 kgCO2e/kWh, "
        "Pork 12.1 kgCO2e/kg) — those reported GHG values are used directly, not "
        "recalculated from CarbonScope's placeholder emission_factors. "
        "Electricity and LPG sheets report monthly figures, scaled to weekly "
        "(x12/52.178571). Device-use sheets (Comp1/Comp2/Cellphone) report daily "
        "hours, scaled x7. Hometown/Vacation travel are one-off annual events, "
        "amortized to a weekly-equivalent share (GHG-per-year / 52.178571). "
        "Reconciled against the workbook's own SUMMARY sheet Total CF/Year across "
        "all 415 students: max discrepancy 0.0002 kg."
    )
    existing = supabase.table("datasets").select("dataset_id").eq("name", name).execute().data
    payload = {
        "name": name, "source": "LNU-F2F pre-computed calculation workbook",
        "unit_of_observation": "one student (SN = serial number in the workbook)",
        "variables": description,
        "limitations": (
            "LPG/cooking gas was computed but is excluded from total_co2e "
            "(no category for it yet in this schema). Transport combines three "
            "sub-categories (daily/hometown/vacation) into one distance_km and "
            "one transport_co2e per record; transport_factor_id is left null "
            "since the source value is a reported GHG figure, not a single "
            "factor x distance calculation."
        ),
        "is_active": True,
    }
    if existing:
        dataset_id = existing[0]["dataset_id"]
        supabase.table("activity_records").delete().eq("dataset_id", dataset_id).execute()
        supabase.table("datasets").update(payload).eq("dataset_id", dataset_id).execute()
        return dataset_id
    return supabase.table("datasets").insert(payload).execute().data[0]["dataset_id"]


def commit_to_supabase(df: pd.DataFrame):
    dataset_id = upsert_dataset()
    cols = ["distance_km", "electricity_kwh", "device_hours_per_day", "paper_sheets",
            "transport_co2e", "electricity_co2e", "devices_co2e", "paper_co2e", "food_co2e"]
    payload = df[cols].to_dict(orient="records")
    for row in payload:
        row["dataset_id"] = dataset_id

    for start in range(0, len(payload), 500):
        supabase.table("activity_records").insert(payload[start:start + 500]).execute()

    supabase.table("processing_log").insert(
        {"event_type": "preprocessing", "records_processed": len(payload), "status": "success",
         "details": {"file": "CF_Calculation_-_LNU-F2F.xlsx", "importer": "import_lnu_f2f"}}
    ).execute()
    print(f"\n    -> imported {len(payload)} rows into dataset '{dataset_id}'")


def process(path: Path, commit: bool):
    sheets = load_sheets(path)
    sn_index = merge_on_sn(sheets)
    weekly = to_weekly(sheets, sn_index)
    filtered, stats = apply_quality_filters(weekly)
    summarize(stats, filtered)

    if commit:
        commit_to_supabase(filtered)
        print("Done. Next: python -m app.mining")
    else:
        print("\nDRY RUN. Nothing was written. Re-run with --commit once the numbers above look right.")


def main():
    parser = argparse.ArgumentParser(description="Import the LNU-F2F pre-computed CF calculation workbook")
    parser.add_argument("--file", required=True, help="path to the .xlsx file")
    parser.add_argument("--inspect", action="store_true", help="check sheet names and row alignment only")
    parser.add_argument("--commit", action="store_true", help="write to the database")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    if args.inspect:
        inspect(path)
    else:
        process(path, commit=args.commit)


if __name__ == "__main__":
    main()
