"""
Bulk import of existing datasets (Excel / CSV) into activity_records.

Follows the study's KDD methodology:
    Selection       -> load_tables()       reads every file in backend/data/
    Preprocessing   -> preprocess_table()  de-duplicates, drops missing/invalid rows
    Transformation  -> transform_table()   maps columns, computes CO2e where needed
    Loading         -> import_file()       writes to Supabase, tagged by dataset

Usage (run from the backend folder, venv activated):
    python -m app.import_data --inspect    show each file's columns and how they match
    python -m app.import_data              dry run: full processing, NOTHING is written
    python -m app.import_data --commit     write to the database

Re-running with --commit replaces the rows of a dataset that was imported
before (matched by file name), so you can fix the settings below and retry.
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from app.database import supabase

DATA_FOLDER = Path(__file__).resolve().parent.parent / "data"
SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv"}
INSERT_CHUNK_SIZE = 500

ACTIVITY_FIELDS = [
    "distance_km",
    "electricity_kwh",
    "device_hours_per_day",
    "paper_sheets",
]
CO2E_FIELDS = [
    "transport_co2e",
    "electricity_co2e",
    "devices_co2e",
    "paper_co2e",
    "food_co2e",
]
NUMERIC_FIELDS = ACTIVITY_FIELDS + CO2E_FIELDS

# 1. COLUMN MATCHING ------------------------------------------------------
# Exact names (after lower-casing and replacing symbols with "_") tried first.
ALIASES = {
    "distance_km": ["distance_km", "distance", "km", "travel_distance", "distance_travelled", "distance_traveled"],
    "electricity_kwh": ["electricity_kwh", "electricity", "kwh", "electricity_use", "electricity_consumption"],
    "device_hours_per_day": ["device_hours_per_day", "device_hours", "screen_time", "digital_device_use"],
    "paper_sheets": ["paper_sheets", "paper", "sheets", "paper_use", "paper_consumption"],
    "transport_mode": ["transport_mode", "transport", "mode_of_transport", "mode_of_transportation", "vehicle_type"],
    "food_pattern": ["food_pattern", "diet", "food", "diet_type", "food_consumption"],
    "transport_co2e": ["transport_co2e", "transport_emission", "transport_emissions"],
    "electricity_co2e": ["electricity_co2e", "electricity_emission", "electricity_emissions"],
    "devices_co2e": ["devices_co2e", "device_co2e", "devices_emission"],
    "paper_co2e": ["paper_co2e", "paper_emission", "paper_emissions"],
    "food_co2e": ["food_co2e", "food_emission", "food_emissions"],
    "submitted_at": ["submitted_at", "date", "timestamp", "datetime", "recorded_at"],
}
# Fallback: a column matches if it contains any of these words
# (columns that mention "co2" only ever match the *_co2e fields).
KEYWORDS = {
    "distance_km": {"distance", "km", "mileage"},
    "electricity_kwh": {"electricity", "kwh", "energy"},
    "device_hours_per_day": {"device", "devices", "gadget", "screen"},
    "paper_sheets": {"paper", "sheets"},
    "transport_mode": {"transport", "transportation", "vehicle", "mode"},
    "food_pattern": {"food", "diet", "meal"},
    "transport_co2e": {"transport", "transportation", "travel", "vehicle"},
    "electricity_co2e": {"electricity", "energy", "power"},
    "devices_co2e": {"device", "devices", "gadget"},
    "paper_co2e": {"paper"},
    "food_co2e": {"food", "diet"},
    "submitted_at": {"date", "timestamp", "datetime"},
}

# 2. LABEL MAPPING --------------------------------------------------------
# Words inside your file's transport / food labels -> our emission_factors subtype.
# Checked top to bottom, first match wins. Add your own words here.
TRANSPORT_LABELS = [
    ("traditional_jeep", ["jeep", "jeepney"]),
    ("motorcycle", ["motor", "scooter"]),
    ("bicycle_walking", ["bicycle", "bike", "walk", "foot"]),
    ("car_gasoline", ["car", "sedan", "suv", "gasoline", "private"]),
]
FOOD_LABELS = [
    ("vegan", ["vegan"]),
    ("mixed_diet", ["non_veg", "nonveg", "omnivore", "mixed", "balanced"]),
    ("vegetarian", ["vegetarian", "veggie"]),
    ("meat_heavy", ["meat", "carnivore"]),
]
# Used when a row's label matches nothing. None = skip those rows instead.
DEFAULT_TRANSPORT_SUBTYPE = None
DEFAULT_FOOD_SUBTYPE = None

# 3. UNITS ----------------------------------------------------------------
# Multiply a field by this to convert to our units (per WEEK, km, kWh, sheets).
# Example: your file has kWh per DAY  ->  "electricity_kwh": 7
UNIT_SCALE = {
    "distance_km": 1,
    "electricity_kwh": 1,
    "device_hours_per_day": 1,
    "paper_sheets": 1,
}

# 4. VALID RANGES ---------------------------------------------------------
# Must match the check constraints in widen_constraints.sql.
LIMITS = {
    "distance_km": (0, 5000),
    "electricity_kwh": (0, 5000),
    "device_hours_per_day": (0, 24),
    "paper_sheets": (0, 5000),
}
CO2E_LIMITS = (0, 99999)


def normalize(name) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def match_columns(columns) -> dict:
    """Returns {our_field: original_column_name} for every column we recognise."""
    normalized = {normalize(c): c for c in columns}
    mapping = {}
    used = set()

    for field, aliases in ALIASES.items():
        for alias in aliases:
            original = normalized.get(alias)
            if original is not None and original not in used:
                mapping[field] = original
                used.add(original)
                break

    for field, words in KEYWORDS.items():
        if field in mapping:
            continue
        for norm, original in normalized.items():
            if original in used:
                continue
            mentions_co2 = "co2" in norm
            if mentions_co2 != field.endswith("_co2e"):
                continue
            if words & set(norm.split("_")):
                mapping[field] = original
                used.add(original)
                break

    return mapping


def label_to_subtype(label, rules, default):
    if label is None or (isinstance(label, float) and pd.isna(label)):
        return default
    text = normalize(label)
    for subtype, words in rules:
        if any(word in text for word in words):
            return subtype
    return default


def load_tables(folder: Path):
    """Phase 1 — Selection: yields (file_path, sheet_name, DataFrame)."""
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS or path.name.startswith("~$"):
            continue
        if path.suffix.lower() == ".csv":
            yield path, "csv", pd.read_csv(path)
        else:
            for sheet_name, frame in pd.read_excel(path, sheet_name=None).items():
                yield path, sheet_name, frame


def preprocess_table(df: pd.DataFrame, mapping: dict, stats: dict) -> pd.DataFrame:
    """Phase 2 — Preprocessing."""
    df = df.dropna(how="all").copy()
    stats["rows_read"] += len(df)

    before = len(df)
    df = df.drop_duplicates()
    stats["duplicates_removed"] += before - len(df)

    for field in NUMERIC_FIELDS:
        if field in mapping:
            df[mapping[field]] = pd.to_numeric(df[mapping[field]], errors="coerce")

    required = [col for field, col in mapping.items() if field != "submitted_at"]
    before = len(df)
    df = df.dropna(subset=required)
    stats["dropped_missing"] += before - len(df)

    keep = pd.Series(True, index=df.index)
    for field in NUMERIC_FIELDS:
        if field not in mapping:
            continue
        low, high = LIMITS.get(field, CO2E_LIMITS)
        scaled = df[mapping[field]] * UNIT_SCALE.get(field, 1)
        keep &= scaled.between(low, high)
    stats["dropped_out_of_range"] += int((~keep).sum())
    return df[keep]


def transform_table(df: pd.DataFrame, mapping: dict, factors: dict, stats: dict) -> pd.DataFrame:
    """Phase 3 — Transformation: standard columns + CO2e (reported or calculated)."""
    out = pd.DataFrame(index=df.index)

    for field in ACTIVITY_FIELDS:
        if field in mapping:
            out[field] = df[mapping[field]] * UNIT_SCALE.get(field, 1)
        else:
            out[field] = 0.0

    def factor_value(category, subtype):
        return factors[(category, subtype)]["factor_value"]

    if "transport_mode" in mapping:
        subtypes = df[mapping["transport_mode"]].map(
            lambda v: label_to_subtype(v, TRANSPORT_LABELS, DEFAULT_TRANSPORT_SUBTYPE)
        )
        for label in df.loc[subtypes.isna(), mapping["transport_mode"]]:
            stats["unmapped_transport"][str(label)] = stats["unmapped_transport"].get(str(label), 0) + 1
    else:
        subtypes = pd.Series(DEFAULT_TRANSPORT_SUBTYPE, index=df.index)

    if "food_pattern" in mapping:
        food_subtypes = df[mapping["food_pattern"]].map(
            lambda v: label_to_subtype(v, FOOD_LABELS, DEFAULT_FOOD_SUBTYPE)
        )
        for label in df.loc[food_subtypes.isna(), mapping["food_pattern"]]:
            stats["unmapped_food"][str(label)] = stats["unmapped_food"].get(str(label), 0) + 1
    else:
        food_subtypes = pd.Series(DEFAULT_FOOD_SUBTYPE, index=df.index)

    def reported_or_calculated(co2e_field, calculated: pd.Series, category: str):
        if co2e_field in mapping:
            stats["co2e_source"][category] = "reported by the source file"
            return df[mapping[co2e_field]]
        if calculated is None:
            stats["co2e_source"][category] = "MISSING (set to 0)"
            return pd.Series(0.0, index=df.index)
        stats["co2e_source"][category] = "calculated with emission_factors"
        return calculated

    transport_calc = None
    if "distance_km" in mapping and subtypes.notna().any():
        transport_calc = out["distance_km"] * subtypes.map(
            lambda s: factor_value("transport", s) if isinstance(s, str) else float("nan")
        )
    out["transport_co2e"] = reported_or_calculated("transport_co2e", transport_calc, "transport")

    electricity_calc = None
    if "electricity_kwh" in mapping:
        electricity_calc = out["electricity_kwh"] * factor_value("electricity", "grid_average")
    out["electricity_co2e"] = reported_or_calculated("electricity_co2e", electricity_calc, "electricity")

    devices_calc = None
    if "device_hours_per_day" in mapping:
        devices_calc = out["device_hours_per_day"] * factor_value("devices", "general_use")
    out["devices_co2e"] = reported_or_calculated("devices_co2e", devices_calc, "devices")

    paper_calc = None
    if "paper_sheets" in mapping:
        paper_calc = out["paper_sheets"] * factor_value("paper", "general_use")
    out["paper_co2e"] = reported_or_calculated("paper_co2e", paper_calc, "paper")

    food_calc = None
    if food_subtypes.notna().any():
        food_calc = food_subtypes.map(
            lambda s: factor_value("food", s) if isinstance(s, str) else float("nan")
        )
    out["food_co2e"] = reported_or_calculated("food_co2e", food_calc, "food")

    before = len(out)
    out = out.dropna(subset=CO2E_FIELDS)
    stats["dropped_unmapped"] += before - len(out)

    if "submitted_at" in mapping:
        parsed = pd.to_datetime(df.loc[out.index, mapping["submitted_at"]], errors="coerce")
        out["submitted_at"] = parsed.fillna(pd.Timestamp.now())

    return out


def new_stats() -> dict:
    return {
        "rows_read": 0,
        "duplicates_removed": 0,
        "dropped_missing": 0,
        "dropped_out_of_range": 0,
        "dropped_unmapped": 0,
        "unmapped_transport": {},
        "unmapped_food": {},
        "co2e_source": {},
    }


def load_factors() -> dict:
    rows = (
        supabase.table("emission_factors")
        .select("factor_id, category, subtype, factor_value")
        .eq("is_active", True)
        .execute()
        .data
    )
    return {(r["category"], r["subtype"]): {**r, "factor_value": float(r["factor_value"])} for r in rows}


def upsert_dataset(name: str, filename: str, description: str) -> str:
    existing = supabase.table("datasets").select("dataset_id").eq("name", name).execute().data
    payload = {
        "name": name,
        "source": f"Imported from {filename}",
        "unit_of_observation": "activity record (one row per observation)",
        "variables": description,
        "limitations": "Auto-imported. Verify units and emission factors against the source documentation.",
        "is_active": True,
    }
    if existing:
        dataset_id = existing[0]["dataset_id"]
        supabase.table("activity_records").delete().eq("dataset_id", dataset_id).execute()
        supabase.table("datasets").update(payload).eq("dataset_id", dataset_id).execute()
        return dataset_id
    return supabase.table("datasets").insert(payload).execute().data[0]["dataset_id"]


def insert_records(dataset_id: str, frame: pd.DataFrame):
    frame = frame.copy()
    frame["dataset_id"] = dataset_id
    rows = json.loads(frame.to_json(orient="records", date_format="iso"))
    for start in range(0, len(rows), INSERT_CHUNK_SIZE):
        supabase.table("activity_records").insert(rows[start:start + INSERT_CHUNK_SIZE]).execute()
    return len(rows)


def print_stats(stats: dict, ready: int):
    print(f"    rows read:                {stats['rows_read']}")
    print(f"    duplicates removed:       {stats['duplicates_removed']}")
    print(f"    dropped (missing values): {stats['dropped_missing']}")
    print(f"    dropped (out of range):   {stats['dropped_out_of_range']}")
    print(f"    dropped (unmapped label): {stats['dropped_unmapped']}")
    print(f"    READY TO IMPORT:          {ready}")
    for category, source in stats["co2e_source"].items():
        print(f"    CO2e {category:<12} -> {source}")
    for kind in ("unmapped_transport", "unmapped_food"):
        if stats[kind]:
            top = sorted(stats[kind].items(), key=lambda kv: -kv[1])[:8]
            print(f"    labels not recognised ({kind.split('_')[1]}): {top}")
            print("      -> add these words to TRANSPORT_LABELS / FOOD_LABELS in import_data.py")


def inspect_files(folder: Path):
    for path, sheet, frame in load_tables(folder):
        mapping = match_columns(frame.columns)
        print(f"\n{path.name}  [sheet: {sheet}]  {len(frame)} rows")
        print(f"  all columns: {list(frame.columns)}")
        for field, column in mapping.items():
            print(f"  matched  {field:<22} <- '{column}'")
        unmatched = [c for c in frame.columns if c not in mapping.values()]
        print(f"  NOT matched (ignored): {unmatched}")


def import_all(folder: Path, commit: bool):
    factors = load_factors()
    per_file = {}
    for path, sheet, frame in load_tables(folder):
        mapping = match_columns(frame.columns)
        has_data = any(f in mapping for f in ACTIVITY_FIELDS + CO2E_FIELDS)
        if not has_data:
            print(f"\n{path.name} [{sheet}]: no recognisable columns, skipped. Run --inspect.")
            continue
        entry = per_file.setdefault(path, {"frames": [], "stats": new_stats(), "mapped": set()})
        cleaned = preprocess_table(frame, mapping, entry["stats"])
        entry["frames"].append(transform_table(cleaned, mapping, factors, entry["stats"]))
        entry["mapped"].update(mapping.keys())

    if not per_file:
        print(f"No usable files found in {folder}")
        return

    grand_total = 0
    for path, entry in per_file.items():
        combined = pd.concat(entry["frames"], ignore_index=True)
        print(f"\n{path.name}")
        print_stats(entry["stats"], len(combined))
        grand_total += len(combined)
        if not commit or combined.empty:
            continue
        description = "; ".join(f"CO2e {k}: {v}" for k, v in entry["stats"]["co2e_source"].items())
        dataset_id = upsert_dataset(path.stem, path.name, description)
        inserted = insert_records(dataset_id, combined)
        supabase.table("processing_log").insert(
            {
                "event_type": "preprocessing",
                "records_processed": inserted,
                "status": "success",
                "details": {"file": path.name, "rows_read": entry["stats"]["rows_read"]},
            }
        ).execute()
        print(f"    -> imported {inserted} rows")

    if commit:
        print(f"\nDone. {grand_total} rows imported. Next: python -m app.mining")
    else:
        print(f"\nDRY RUN: {grand_total} rows would be imported. Nothing was written.")
        print("If the numbers look right, run again with --commit")


def main():
    parser = argparse.ArgumentParser(description="Import existing datasets into CarbonScope")
    parser.add_argument("--inspect", action="store_true", help="show columns and matches only")
    parser.add_argument("--commit", action="store_true", help="write to the database")
    parser.add_argument("--folder", default=str(DATA_FOLDER), help="folder with the Excel/CSV files")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.exists():
        print(f"Folder not found: {folder}\nCreate it and put your Excel files inside.")
        return
    if args.inspect:
        inspect_files(folder)
    else:
        import_all(folder, commit=args.commit)


if __name__ == "__main__":
    main()
