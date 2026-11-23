"""Create a bus-stop list indexed by STAGE_NO from the TNSTC geofence workbook."""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import load_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_BUS_DETAILS_DIR = PROJECT_ROOT / "data" / "raw bus details"
SOURCE_FILE = RAW_BUS_DETAILS_DIR / "GEOFENCE CODE.xlsx"
OUTPUT_FILE = PROJECT_ROOT / "data" / "final bus details" / "unique_stops.csv"


def normalise_header(value: object) -> str:
    """Make header matching insensitive to surrounding spaces and case."""
    return " ".join(str(value or "").strip().upper().split())


def main() -> None:
    workbook = load_workbook(SOURCE_FILE, read_only=True, data_only=True)
    try:
        sheet = workbook["GEOCODES"]
        rows = sheet.iter_rows(values_only=True)

        # Locate the source headers instead of relying on their column positions.
        for headers in rows:
            column_lookup = {
                normalise_header(header): index for index, header in enumerate(headers)
            }
            if {
                "STAGE_NO",
                "STAGE_NAME",
                "GEOCODE",
            }.issubset(column_lookup):
                break
        else:
            raise ValueError(
                "STAGE_NO, STAGE_NAME, and GEOCODE headers were not found in GEOCODES."
            )

        stage_column = column_lookup["STAGE_NO"]
        stop_column = column_lookup["STAGE_NAME"]
        geocode_column = column_lookup["GEOCODE"]
        stops: dict[int, tuple[str, float, float]] = {}

        for row in rows:
            stage_number = row[stage_column]
            stop_name = str(row[stop_column] or "").strip()
            geocode = str(row[geocode_column] or "").strip()
            if stage_number is None or not stop_name or not geocode:
                continue

            try:
                stage_number = int(stage_number)
                latitude_text, longitude_text = (part.strip() for part in geocode.split(","))
                latitude = float(latitude_text)
                longitude = float(longitude_text)
            except ValueError:
                # Ignore malformed or incomplete geocodes rather than emitting
                # an unusable stop record.
                continue

            # Each STAGE_NO identifies one output stop. Keep its first valid
            # source record, preserving the workbook's original order.
            stops.setdefault(stage_number, (stop_name, latitude, longitude))
    finally:
        workbook.close()

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output, fieldnames=["STOP NAME", "LATITUDE", "LONGITUDE"]
        )
        writer.writeheader()
        for stage_number in sorted(stops):
            stop_name, latitude, longitude = stops[stage_number]
            writer.writerow(
                {
                    "STOP NAME": stop_name,
                    "LATITUDE": latitude,
                    "LONGITUDE": longitude,
                }
            )

    print(f"Wrote {len(stops)} unique bus stops to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
