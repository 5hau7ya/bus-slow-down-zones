"""Clean raw GPS records in stages.

Stage 1 removes impossible coordinates: a record is discarded when either its
latitude or longitude is zero. The raw files are never modified; cleaned CSVs
are written to an equivalent dated-folder structure under ``data/clean gps``.

The cleaned files retain only the fields needed to identify repeatable bus
slowdowns: when and where each bus was observed, its identifier, and its speed.
Long zero-speed runs at file boundaries are condensed to their boundary records
so they do not dominate the subsequent slowdown-zone analysis.
"""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_GPS_DATA_DIR = PROJECT_ROOT / "data" / "raw gps"
CLEANED_GPS_DATA_DIR = PROJECT_ROOT / "data" / "clean gps"

# Broad geographic bounds around the study area, in decimal degrees.
MIN_LATITUDE = Decimal("7")
MAX_LATITUDE = Decimal("10.5")
MIN_LONGITUDE = Decimal("75")
MAX_LONGITUDE = Decimal("85")

# Output names follow the all-caps convention used by the project's other
# generated datasets. Device time is preferred over server time because it is
# the time reported for the GPS observation itself.
OUTPUT_COLUMNS = {
    "Device_Timestamp": "DEVICE TIMESTAMP",
    "IMEI": "DEVICE IMEI",
    "Latitude": "LATITUDE",
    "Longitude": "LONGITUDE",
    "Speed": "SPEED",
}


def is_zero_coordinate(value: str | None) -> bool:
    """Return whether a CSV coordinate represents numeric zero.

    Decimal treats values such as ``0``, ``0.0``, and ``-0.000`` uniformly,
    while malformed or blank values are left for a later cleaning stage.
    """
    if value is None or not value.strip():
        return False

    try:
        return Decimal(value.strip()) == 0
    except InvalidOperation:
        return False


def is_outside_study_area(latitude: str | None, longitude: str | None) -> bool:
    """Return whether valid numeric coordinates lie outside the study bounds."""
    try:
        latitude_value = Decimal(str(latitude).strip())
        longitude_value = Decimal(str(longitude).strip())
    except (InvalidOperation, ValueError):
        # Missing or malformed values are not covered by this cleaning stage.
        return False

    if not latitude_value.is_finite() or not longitude_value.is_finite():
        return False

    return not (
        MIN_LATITUDE <= latitude_value <= MAX_LATITUDE
        and MIN_LONGITUDE <= longitude_value <= MAX_LONGITUDE
    )


def is_zero_speed(value: str | None) -> bool:
    """Return whether a speed value represents numeric zero."""
    if value is None or not value.strip():
        return False

    try:
        return Decimal(value.strip()) == 0
    except InvalidOperation:
        return False


def removal_reason(row: dict[str, str | None]) -> str | None:
    """Return the coordinate-filter removal reason, if the row is discarded."""
    if is_zero_coordinate(row["Latitude"]) or is_zero_coordinate(row["Longitude"]):
        return "zero_coordinate"
    if is_outside_study_area(row["Latitude"], row["Longitude"]):
        return "out_of_bounds"
    return None


def find_endpoint_zero_run_boundaries(source_file: Path) -> tuple[int | None, int | None]:
    """Return the retained indexes for the initial and final zero-speed runs.

    Indexes refer only to rows that survived the coordinate filters. For a file
    made entirely of zero-speed rows, both its first and last row are retained
    to preserve the observed time span.
    """
    start_run_active = True
    last_start_zero_index: int | None = None
    first_end_zero_index: int | None = None
    retained_index = 0

    with source_file.open("r", newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            if removal_reason(row) is not None:
                continue

            if is_zero_speed(row["Speed"]):
                if start_run_active:
                    last_start_zero_index = retained_index
                if first_end_zero_index is None:
                    first_end_zero_index = retained_index
            else:
                start_run_active = False
                first_end_zero_index = None

            retained_index += 1

    return last_start_zero_index, first_end_zero_index


def should_remove_endpoint_zero_row(
    row_index: int,
    last_start_zero_index: int | None,
    first_end_zero_index: int | None,
) -> bool:
    """Return whether an endpoint zero-speed row is redundant.

    The explicit exceptions preserve both ends when one zero-speed run spans an
    entire file.
    """
    if (
        last_start_zero_index is not None
        and row_index < last_start_zero_index
        and row_index != first_end_zero_index
    ):
        return True
    if (
        first_end_zero_index is not None
        and row_index > first_end_zero_index
        and row_index != last_start_zero_index
    ):
        return True
    return False


def clean_file(source_file: Path, destination_file: Path) -> tuple[int, int, int, int]:
    """Write one cleaned GPS file and return kept and removal counts."""
    with source_file.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError(f"GPS file has no header row: {source_file}")

        required_columns = set(OUTPUT_COLUMNS)
        missing_columns = required_columns.difference(reader.fieldnames)
        if missing_columns:
            missing = ", ".join(sorted(missing_columns))
            raise ValueError(f"GPS file is missing {missing}: {source_file}")

    last_start_zero_index, first_end_zero_index = find_endpoint_zero_run_boundaries(
        source_file
    )

    destination_file.parent.mkdir(parents=True, exist_ok=True)
    with (
        source_file.open("r", newline="", encoding="utf-8-sig") as source,
        destination_file.open("w", newline="", encoding="utf-8") as destination,
    ):
        reader = csv.DictReader(source)
        writer = csv.DictWriter(destination, fieldnames=OUTPUT_COLUMNS.values())
        writer.writeheader()

        kept_rows = 0
        zero_coordinate_rows = 0
        out_of_bounds_rows = 0
        endpoint_zero_rows = 0
        retained_index = 0
        for row in reader:
            reason = removal_reason(row)
            if reason == "zero_coordinate":
                zero_coordinate_rows += 1
                continue
            if reason == "out_of_bounds":
                out_of_bounds_rows += 1
                continue

            if should_remove_endpoint_zero_row(
                retained_index, last_start_zero_index, first_end_zero_index
            ):
                endpoint_zero_rows += 1
                retained_index += 1
                continue

            writer.writerow(
                {
                    output_name: row[source_name]
                    for source_name, output_name in OUTPUT_COLUMNS.items()
                }
            )
            kept_rows += 1
            retained_index += 1

    return kept_rows, zero_coordinate_rows, out_of_bounds_rows, endpoint_zero_rows


def main() -> None:
    """Apply stage 1 to every raw GPS CSV, retaining the source layout."""
    if not RAW_GPS_DATA_DIR.is_dir():
        raise FileNotFoundError(f"Raw GPS directory was not found: {RAW_GPS_DATA_DIR}")

    processed_files = 0
    kept_rows = 0
    zero_coordinate_rows = 0
    out_of_bounds_rows = 0
    endpoint_zero_rows = 0

    for source_file in sorted(RAW_GPS_DATA_DIR.rglob("*.csv")):
        relative_path = source_file.relative_to(RAW_GPS_DATA_DIR)
        (
            kept,
            removed_for_zero_coordinates,
            removed_for_bounds,
            removed_for_endpoint_zero_runs,
        ) = clean_file(
            source_file, CLEANED_GPS_DATA_DIR / relative_path
        )
        processed_files += 1
        kept_rows += kept
        zero_coordinate_rows += removed_for_zero_coordinates
        out_of_bounds_rows += removed_for_bounds
        endpoint_zero_rows += removed_for_endpoint_zero_runs

    dropped_rows = zero_coordinate_rows + out_of_bounds_rows + endpoint_zero_rows

    print(
        f"Processed {processed_files} GPS files: kept {kept_rows:,} rows and "
        f"dropped {dropped_rows:,} rows "
        f"({zero_coordinate_rows:,} with a zero latitude or longitude; "
        f"{out_of_bounds_rows:,} outside the study bounds; "
        f"{endpoint_zero_rows:,} redundant endpoint zero-speed rows)."
    )


if __name__ == "__main__":
    main()
