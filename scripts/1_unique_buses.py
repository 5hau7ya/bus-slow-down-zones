"""Create a unique bus list from the GPS-data filenames.

Each GPS CSV is named after its device IMEI. The script gathers those names
across every dated folder under ``data/raw gps`` and maps them to vehicle
numbers, giving the first mapping workbook priority over the second one.
"""

from __future__ import annotations
import csv
from pathlib import Path
from typing import Iterable
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_GPS_DATA_DIR = PROJECT_ROOT / "data" / "raw gps"
RAW_BUS_DETAILS_DIR = PROJECT_ROOT / "data" / "raw bus details"
PRIMARY_MAPPING_FILE = RAW_BUS_DETAILS_DIR / "IITM Thoothukudi-GPS _MTC number.xlsx"
SECONDARY_MAPPING_FILE = RAW_BUS_DETAILS_DIR / "IITM Thoothukudi-GPS _MTC number 2.xlsx"
OUTPUT_FILE = PROJECT_ROOT / "data" / "final bus details" / "unique_buses.csv"

def normalise(value: object) -> str:
    """Return a trimmed string, keeping Excel numeric IMEIs comparable."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def standardise_vehicle_number(value: object) -> str:
    """Return a vehicle number with every whitespace character removed."""
    return "".join(normalise(value).split())

def find_column(headers: Iterable[object], *expected_names: str) -> int:
    """Find a column without depending on header case or spacing."""
    targets = {"".join(name.lower().split()) for name in expected_names}
    for index, header in enumerate(headers):
        candidate = "".join(normalise(header).lower().split())
        if candidate in targets:
            return index
    names = " or ".join(repr(name) for name in expected_names)
    raise ValueError(f"Column {names} was not found in the mapping workbook.")

def read_vehicle_mapping(path: Path) -> dict[str, str]:
    """Read Device IEMI/IMEI to Vehicle Number values from one workbook."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        headers = next(rows, None)
        if headers is None:
            raise ValueError(f"Mapping workbook is empty: {path.name}")

        # The supplied workbooks spell this header as "Device IEMI"; accepting
        # the conventional spelling also keeps the script usable with a corrected
        # export of the same data.
        imei_column = find_column(headers, "Device IEMI", "Device IMEI")
        vehicle_column = find_column(headers, "Vehicle Number")
        mapping: dict[str, str] = {}
        for row in rows:
            imei = normalise(row[imei_column] if imei_column < len(row) else None)
            vehicle_number = standardise_vehicle_number(
                row[vehicle_column] if vehicle_column < len(row) else None
            )
            # Presence in the primary file is decisive, even if its vehicle
            # number is empty: the secondary file must not then be consulted.
            if imei and imei not in mapping:
                mapping[imei] = vehicle_number
        return mapping
    finally:
        workbook.close()


def get_gps_imeis(gps_data_dir: Path) -> dict[str, int]:
    """Return each IMEI and the number of dated folders in which it appears."""
    if not gps_data_dir.is_dir():
        raise FileNotFoundError(f"Raw GPS directory was not found: {gps_data_dir}")

    imei_dates: dict[str, set[str]] = {}
    for date_folder in gps_data_dir.iterdir():
        if not date_folder.is_dir():
            continue
        for gps_file in date_folder.glob("*.csv"):
            imei = gps_file.stem.strip()
            if imei:
                imei_dates.setdefault(imei, set()).add(date_folder.name)

    return {imei: len(imei_dates[imei]) for imei in sorted(imei_dates)}


def main() -> None:
    primary_mapping = read_vehicle_mapping(PRIMARY_MAPPING_FILE)
    secondary_mapping = read_vehicle_mapping(SECONDARY_MAPPING_FILE)
    imei_days = get_gps_imeis(RAW_GPS_DATA_DIR)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output, fieldnames=["DEVICE IMEI", "VEHICLE NUMBER", "DAYS"]
        )
        writer.writeheader()
        for imei, days in imei_days.items():
            # The secondary file is consulted only when the IMEI is absent from
            # the primary file, as required.
            vehicle_number = primary_mapping.get(imei)
            if vehicle_number is None:
                vehicle_number = secondary_mapping.get(imei, "")
            # Keep the output invariant even if this mapping is later supplied
            # by a different source than ``read_vehicle_mapping``.
            vehicle_number = standardise_vehicle_number(vehicle_number)
            writer.writerow(
                {
                    "DEVICE IMEI": imei,
                    "VEHICLE NUMBER": vehicle_number,
                    "DAYS": days,
                }
            )

    print(f"Wrote {len(imei_days)} unique device IMEIs to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
