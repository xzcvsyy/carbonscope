"""
Converts the LNU Carbon Footprint Survey (Google Forms export, 58 columns)
into the flat format that import_data.py already understands.

Why this exists: the survey asks about *behaviour* (meals, electricity bill in
pesos, how many pages printed, ...), while CarbonScope's activity_records
expects one row per record with weekly activity values. This script only
reshapes the survey into those activity values. It does NOT compute CO2e --
import_data.py does that afterwards using the same emission_factors table as
the Estimate page, so live estimates and survey data stay on the same scale.

Output columns (all names match import_data.py's ALIASES exactly):
    submitted_at, distance_km, transport_mode, electricity_kwh,
    device_hours_per_day, paper_sheets, food_pattern

Personal data (name, contact number, email, address) is never copied out.

Usage (run from the backend folder, venv activated):
    1. Put the raw survey .xlsx in  backend/data/raw/   (NOT in backend/data/)
    2. python -m app.convert_survey
    3. python -m app.import_data --inspect
    4. python -m app.import_data            (dry run)
    5. python -m app.import_data --commit
    6. python -m app.mining
"""

import argparse
import re
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_FOLDER = BASE_DIR / "data" / "raw"
OUTPUT_FILE = BASE_DIR / "data" / "lnu_carbon_survey_clean.csv"

# ---------------------------------------------------------------------------
# ASSUMPTIONS -- change these, re-run, and re-import if your study differs
# ---------------------------------------------------------------------------
# The survey asks for the monthly bill in pesos, not kWh. Average residential
# rate in Leyte during 2021-2022 (adjust to the rate you cite in the paper).
ELECTRICITY_RATE_PHP_PER_KWH = 10.0
WEEKS_PER_MONTH = 52 / 12

# "How many times do you commute per week (back and forth)" counts round trips;
# "How far do you commute" is read as ONE-WAY distance. Set to 1 if your
# respondents gave round-trip distance instead.
COMMUTE_ROUND_TRIP_FACTOR = 2

# Food: the app only knows diet patterns, so the survey's pork / chicken / fish
# frequencies are turned into one. If at least this share of a person's weekly
# meals contain pork, chicken or fish, they are "meat heavy"; otherwise "mixed".
# No meat/fish at all -> "vegetarian".
MEAT_HEAVY_SHARE = 0.5
DEFAULT_MEALS_PER_DAY = 3  # used when the answer is free text like "As much as I want"

# Laptop hours + smartphone hours are added together, but people use both at the
# same time, so totals can exceed 24 hours. Totals above the cap are set to the
# cap instead of losing the respondent. Set to None to keep raw totals (import_data.py
# will then drop every row above 24).
DEVICE_HOURS_CAP = 24

# Survey answer -> a word import_data.py's TRANSPORT_LABELS already recognises.
# The app only has 4 transport subtypes, so shared public transport
# (multicab, shuttle bus, van, bus) is treated as "traditional_jeep".
MODE_LABELS = [
    ("personal car", "Car"),
    ("car", "Car"),
    ("motor", "Motorcycle"),
    ("tricycle", "Motorcycle"),
    ("multicab", "Jeepney"),
    ("jeep", "Jeepney"),
    ("shuttle", "Jeepney"),
    ("bus", "Jeepney"),
    ("van", "Jeepney"),
    ("bicycle", "Bicycle"),
    ("bike", "Bicycle"),
    ("walk", "Walking"),
]

# Same ranges as import_data.py LIMITS -- used only to WARN, not to drop.
LIMITS = {
    "distance_km": 5000,
    "electricity_kwh": 5000,
    "device_hours_per_day": 24,
    "paper_sheets": 5000,
}

# ---------------------------------------------------------------------------
# Column lookup: unique fragment of each question's header text
# ---------------------------------------------------------------------------
QUESTIONS = {
    "timestamp": "Timestamp",
    "members": "Number of household members",
    "bill": "How much do you pay for your electricity bill",
    "meals": "How many meals do you have everyday",
    "pork": "times a week do you eat pork",
    "chicken": "times a week do you eat chicken",
    "fish": "times a week do you eat fish",
    "laptop_fun": "for entertainment every day",
    "laptop_study": "for studying every day",
    "phone": "smartphone for school works",
    "print": "for printing every week",
    "scan": "for scanning/photocopying every week",
    "commutes": "Do you commute to school during",
    "commute_times": "times do you commute to school per week",
    "commute_km": "How far do you commute to school",
    "commute_mode": "How do you commute to school",
}


def find_columns(df: pd.DataFrame) -> dict:
    found = {}
    for key, fragment in QUESTIONS.items():
        matches = [c for c in df.columns if fragment.lower() in str(c).lower()]
        if len(matches) != 1:
            raise SystemExit(
                f"Expected exactly 1 column containing '{fragment}' ({key}), "
                f"found {len(matches)}. Has the survey form changed?"
            )
        found[key] = matches[0]
    return found


# ---------------------------------------------------------------------------
# Parsers for the messy answers
# ---------------------------------------------------------------------------
def parse_frequency(value):
    """'Once a week' / '5 times a week' / 'more than 6 times a week' -> times per week."""
    if pd.isna(value):
        return None
    text = str(value).strip().lower()
    number = re.search(r"\d+", text)
    if text.startswith("more than"):
        return int(number.group()) + 1 if number else None
    if number:
        return int(number.group())
    for word, times in (("once", 1), ("twice", 2), ("thrice", 3), ("four", 4)):
        if word in text:
            return times
    return None


def parse_pages(value):
    """'0-5' -> 2.5, '11-20' -> 15.5, 'More than 50 pages' -> 60."""
    if pd.isna(value):
        return None
    text = str(value).strip().lower()
    number = re.search(r"\d+", text)
    if text.startswith("more than"):
        return float(number.group()) + 10 if number else None
    span = re.match(r"(\d+)\s*-\s*(\d+)", text)
    if span:
        return (int(span.group(1)) + int(span.group(2))) / 2
    return float(number.group()) if number else None


def parse_meals(value):
    """Mostly a number; also handles '2 to 3', 'Four', 'Sometimes 2 sometimes 3'."""
    if pd.isna(value):
        return DEFAULT_MEALS_PER_DAY
    numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(value))]
    if numbers:
        return min(max(sum(numbers) / len(numbers), 1), 6)
    if "four" in str(value).lower():
        return 4.0
    return DEFAULT_MEALS_PER_DAY


def parse_yes_no(value):
    if pd.isna(value):
        return None
    text = str(value).strip().lower()
    if text.startswith("y"):
        return True
    if text.startswith("n"):
        return False
    return None


def mode_label(value):
    if pd.isna(value):
        return None
    text = str(value).strip().lower()
    for word, label in MODE_LABELS:
        if word in text:
            return label
    return None


def diet_pattern(row):
    events = [row["pork_freq"], row["chicken_freq"], row["fish_freq"]]
    if any(e is None or pd.isna(e) for e in events):
        return None
    meat_meals = sum(events)
    if meat_meals == 0:
        return "Vegetarian"
    share = min(meat_meals / (row["meals_per_day"] * 7), 1.0)
    return "Meat heavy" if share >= MEAT_HEAVY_SHARE else "Mixed diet"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def convert(raw: pd.DataFrame) -> pd.DataFrame:
    col = find_columns(raw)
    num = lambda key: pd.to_numeric(raw[col[key]], errors="coerce")

    out = pd.DataFrame(index=raw.index)
    out["submitted_at"] = pd.to_datetime(raw[col["timestamp"]], errors="coerce")

    # Electricity: pesos/month -> kWh/month -> per person -> per week
    members = num("members")
    members = members.where(members >= 1)
    out["electricity_kwh"] = (
        num("bill") / ELECTRICITY_RATE_PHP_PER_KWH / members / WEEKS_PER_MONTH
    ).round(2)

    # Devices: laptop/PC (fun + study) + smartphone, hours per day.
    # Blank laptop answers mean "no laptop", so they count as 0 hours.
    device_hours = num("laptop_fun").fillna(0) + num("laptop_study").fillna(0) + num("phone")
    if DEVICE_HOURS_CAP is not None:
        device_hours = device_hours.clip(upper=DEVICE_HOURS_CAP)
    out["device_hours_per_day"] = device_hours.round(2)

    # Paper: printed + scanned/photocopied pages per week (bucket midpoints)
    out["paper_sheets"] = (
        raw[col["print"]].map(parse_pages) + raw[col["scan"]].map(parse_pages)
    ).astype(float)

    # Transport: school commute only (see notes in the summary)
    commutes = raw[col["commutes"]].map(parse_yes_no)
    weekly_km = num("commute_times") * num("commute_km") * COMMUTE_ROUND_TRIP_FACTOR
    mode = raw[col["commute_mode"]].map(mode_label)
    out["distance_km"] = pd.Series(float("nan"), index=raw.index)
    out["transport_mode"] = pd.Series(None, index=raw.index, dtype="object")
    for idx, does_commute in commutes.items():
        if does_commute is False:
            out.at[idx, "distance_km"] = 0.0
            out.at[idx, "transport_mode"] = "Walking"  # 0 km, so the factor is irrelevant
        elif does_commute is True:
            out.at[idx, "distance_km"] = round(weekly_km[idx], 2) if pd.notna(weekly_km[idx]) else float("nan")
            out.at[idx, "transport_mode"] = mode[idx]

    # Food pattern
    diet_inputs = pd.DataFrame(
        {
            "pork_freq": raw[col["pork"]].map(parse_frequency),
            "chicken_freq": raw[col["chicken"]].map(parse_frequency),
            "fish_freq": raw[col["fish"]].map(parse_frequency),
            "meals_per_day": raw[col["meals"]].map(parse_meals),
        }
    )
    out["food_pattern"] = diet_inputs.apply(diet_pattern, axis=1)

    return out[
        [
            "submitted_at",
            "distance_km",
            "transport_mode",
            "electricity_kwh",
            "device_hours_per_day",
            "paper_sheets",
            "food_pattern",
        ]
    ]


def print_summary(out: pd.DataFrame, total_rows: int):
    complete = out.dropna()
    print(f"\nRows in survey:              {total_rows}")
    print(f"Rows with every field:       {len(complete)}")
    print(f"Rows with a blank field:     {total_rows - len(complete)}  (import_data.py will count these as 'dropped missing')")

    print("\nBlank fields per column:")
    for column, count in out.isna().sum().items():
        if count:
            print(f"    {column:<22} {count}")

    over = {f: int((out[f] > limit).sum()) for f, limit in LIMITS.items()}
    if any(over.values()):
        print("\nOver the allowed range (import_data.py will drop these):")
        for field, count in over.items():
            if count:
                print(f"    {field:<22} {count}  (limit {LIMITS[field]})")

    print("\nDiet pattern:")
    print(out["food_pattern"].value_counts(dropna=False).to_string())
    print("\nTransport mode (non-commuters are set to 'Walking' at 0 km):")
    print(out["transport_mode"].value_counts(dropna=False).to_string())
    print("\nWeekly values (complete rows):")
    print(complete.describe().round(2).T[["mean", "50%", "max"]].to_string())

    print(
        "\nNot converted, because the app has no matching field:"
        "\n    LPG use, hometown trips (no distance asked), vacation trips (free-text distance)."
        "\nAssumptions in use:"
        f"\n    electricity rate = PHP {ELECTRICITY_RATE_PHP_PER_KWH}/kWh, split per household member"
        f"\n    commute distance x{COMMUTE_ROUND_TRIP_FACTOR} (one-way km, round trips per week)"
        f"\n    device hours capped at {DEVICE_HOURS_CAP}/day (laptop + phone can overlap)"
        f"\n    'meat heavy' = at least {MEAT_HEAVY_SHARE:.0%} of weekly meals include pork/chicken/fish"
    )


def find_input() -> Path:
    files = sorted(
        p for p in RAW_FOLDER.glob("*.xls*") if not p.name.startswith("~$")
    )
    if not files:
        raise SystemExit(f"No Excel file found in {RAW_FOLDER}. Put the raw survey export there.")
    return files[0]


def main():
    parser = argparse.ArgumentParser(description="Convert the LNU survey export for CarbonScope")
    parser.add_argument("--input", help="path to the survey .xlsx (default: first file in data/raw/)")
    parser.add_argument("--output", default=str(OUTPUT_FILE), help="where to write the cleaned CSV")
    args = parser.parse_args()

    source = Path(args.input) if args.input else find_input()
    raw = pd.read_excel(source)
    print(f"Reading {source.name}: {len(raw)} rows, {raw.shape[1]} columns")

    out = convert(raw)
    print_summary(out, len(raw))

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(destination, index=False)
    print(f"\nWrote {destination}")
    print("Next: python -m app.import_data --inspect")


if __name__ == "__main__":
    main()
