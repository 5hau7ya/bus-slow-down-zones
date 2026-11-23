"""Remove GPS observations caused by bus-stop slowdowns.

For every cleaned GPS file, this stage assigns each observation to its nearest
bus stop when it lies in that stop's circular buffer.  A run in a stop buffer
is removed only when its minimum speed is substantially lower than the nearby
approach/departure speed.  This keeps ordinary drive-through observations while
removing observations that are plausibly the result of servicing a stop.

The original files under ``data/clean gps`` are never changed. Filtered GPS
data is written under ``data/final gps`` with the same date/IMEI layout. For
each mapped vehicle, ``results/stop time/stop_time_<vehicle number>.csv``
lists the slowed stop visits. Rows are bus stops and columns are dates; a cell
has one or more ``start-end (duration)`` entries for that date.

Adjust ``STOP_BUFFER_RADIUS_METRES`` to experiment with the buffer size.
"""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
FINAL_BUS_DETAILS_DIR = DATA_DIR / "final bus details"
CLEANED_GPS_DATA_DIR = DATA_DIR / "clean gps"
STOPS_FILE = FINAL_BUS_DETAILS_DIR / "unique_stops.csv"
VEHICLES_FILE = FINAL_BUS_DETAILS_DIR / "unique_buses.csv"
FILTERED_GPS_DATA_DIR = DATA_DIR / "final gps"
STOP_EFFECT_DIR = PROJECT_ROOT / "results" / "stop time"

# This is intentionally a single, obvious experiment control requested for the
# stop-zone analysis.  A point exactly on the boundary is included.
STOP_BUFFER_RADIUS_METRES = 50.0

# A stop slowdown is identified relative to nearby movement, rather than a
# fixed speed alone.  This avoids removing a bus that is already travelling
# slowly because of traffic before reaching the stop.
SLOWDOWN_DROP_KMPH = 10.0
REFERENCE_POINTS_EACH_SIDE = 3
MAXIMUM_EVENT_GAP = timedelta(minutes=5)

GPS_COLUMNS = (
    "DEVICE TIMESTAMP",
    "DEVICE IMEI",
    "LATITUDE",
    "LONGITUDE",
    "SPEED",
)
EARTH_RADIUS_METRES = 6_371_000.0


@dataclass(frozen=True)
class Stop:
    """A named bus stop represented by its geographic centre."""

    name: str
    label: str
    latitude: float
    longitude: float


@dataclass
class GPSRecord:
    """A valid cleaned-GPS row plus the values needed by this stage."""

    row: dict[str, str]
    timestamp: datetime
    latitude: float
    longitude: float
    speed: float | None
    source_index: int
    stop_name: str | None = None


@dataclass(frozen=True)
class StopVisit:
    """A contiguous, slowed visit to one bus-stop buffer."""

    stop_name: str
    start: datetime
    end: datetime
    record_indexes: tuple[int, ...]

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def parse_timestamp(value: str) -> datetime:
    """Parse the timestamp formats emitted by the GPS exports."""
    value = value.strip()
    for timestamp_format in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, timestamp_format)
        except ValueError:
            pass
    raise ValueError(f"invalid DEVICE TIMESTAMP: {value!r}")


def haversine_metres(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    """Return the great-circle distance between two latitude/longitude points."""
    latitude_delta = math.radians(latitude_b - latitude_a)
    longitude_delta = math.radians(longitude_b - longitude_a)
    latitude_a_radians = math.radians(latitude_a)
    latitude_b_radians = math.radians(latitude_b)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_a_radians)
        * math.cos(latitude_b_radians)
        * math.sin(longitude_delta / 2) ** 2
    )
    return EARTH_RADIUS_METRES * 2 * math.asin(math.sqrt(haversine))


def read_stops(path: Path) -> list[Stop]:
    """Read the bus-stop centres from the previous pipeline stage."""
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"STOP NAME", "LATITUDE", "LONGITUDE"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} must contain {sorted(required)}")

        raw_stops: list[tuple[str, float, float]] = []
        for row in reader:
            try:
                raw_stops.append(
                    (
                        row["STOP NAME"].strip(),
                        float(row["LATITUDE"]),
                        float(row["LONGITUDE"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    if not raw_stops:
        raise ValueError(f"No valid stops were found in {path}")

    # Two labels in the source occur at different coordinates.  Add a compact
    # coordinate suffix only to repeated names so every matrix row describes a
    # unique physical stop while ordinary names remain easy to read.
    name_counts: dict[str, int] = defaultdict(int)
    for name, _, _ in raw_stops:
        name_counts[name] += 1
    stops = [
        Stop(
            name=name,
            label=(f"{name} ({latitude:.6f}, {longitude:.6f})"
                   if name_counts[name] > 1 else name),
            latitude=latitude,
            longitude=longitude,
        )
        for name, latitude, longitude in raw_stops
    ]
    return stops


def read_vehicle_numbers(path: Path) -> dict[str, str]:
    """Read IMEI-to-vehicle mapping, falling back to IMEI when unmapped."""
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        required = {"DEVICE IMEI", "VEHICLE NUMBER"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} must contain {sorted(required)}")
        return {
            row["DEVICE IMEI"].strip(): row["VEHICLE NUMBER"].strip()
            for row in reader
            if row["DEVICE IMEI"].strip()
        }


def read_gps_records(path: Path) -> tuple[list[GPSRecord], list[dict[str, str]]]:
    """Read usable records and retain malformed rows for the filtered output."""
    with path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not set(GPS_COLUMNS).issubset(reader.fieldnames):
            raise ValueError(f"{path} is missing one or more cleaned GPS columns")
        rows = list(reader)

    records: list[GPSRecord] = []
    for source_index, row in enumerate(rows):
        try:
            records.append(
                GPSRecord(
                    row=row,
                    timestamp=parse_timestamp(row["DEVICE TIMESTAMP"]),
                    latitude=float(row["LATITUDE"]),
                    longitude=float(row["LONGITUDE"]),
                    speed=float(row["SPEED"]) if row["SPEED"].strip() else None,
                    source_index=source_index,
                )
            )
        except (KeyError, TypeError, ValueError):
            # Do not silently discard a row merely because it cannot take part
            # in distance/speed calculations.
            continue
    return records, rows


def assign_nearest_stop(records: list[GPSRecord], stops: list[Stop]) -> None:
    """Set ``stop_name`` for records in a buffer, using nearest-stop tie-breaks."""
    for record in records:
        nearest_stop, nearest_distance = min(
            (
                (stop, haversine_metres(record.latitude, record.longitude, stop.latitude, stop.longitude))
                for stop in stops
            ),
            key=lambda candidate: candidate[1],
        )
        if nearest_distance <= STOP_BUFFER_RADIUS_METRES:
            record.stop_name = nearest_stop.label


def contiguous_stop_segments(records: list[GPSRecord]) -> list[list[int]]:
    """Group consecutive timestamp-ordered observations in the same buffer."""
    segments: list[list[int]] = []
    active: list[int] = []
    for record_index, record in enumerate(records):
        continues_active = (
            active
            and record.stop_name is not None
            and record.stop_name == records[active[-1]].stop_name
            and record.timestamp - records[active[-1]].timestamp <= MAXIMUM_EVENT_GAP
        )
        if continues_active:
            active.append(record_index)
        else:
            if active:
                segments.append(active)
            active = [record_index] if record.stop_name is not None else []
    if active:
        segments.append(active)
    return segments


def reference_speeds(records: list[GPSRecord], segment: list[int]) -> list[float]:
    """Collect valid speeds immediately outside a stop-buffer segment."""
    speeds: list[float] = []
    first_index, last_index = segment[0], segment[-1]
    for record in records[max(0, first_index - REFERENCE_POINTS_EACH_SIDE) : first_index]:
        if record.speed is not None:
            speeds.append(record.speed)
    for record in records[last_index + 1 : last_index + 1 + REFERENCE_POINTS_EACH_SIDE]:
        if record.speed is not None:
            speeds.append(record.speed)
    return speeds


def slowed_stop_visits(records: list[GPSRecord]) -> list[StopVisit]:
    """Return only stop-buffer visits whose speed drops versus nearby movement."""
    visits: list[StopVisit] = []
    for segment in contiguous_stop_segments(records):
        in_zone_speeds = [records[index].speed for index in segment]
        valid_in_zone_speeds = [speed for speed in in_zone_speeds if speed is not None]
        nearby_speeds = reference_speeds(records, segment)
        if not valid_in_zone_speeds or not nearby_speeds:
            continue

        # The minimum finds a genuine braking/stopping point, while the median
        # reference is resilient to a single noisy GPS speed observation.
        if median(nearby_speeds) - min(valid_in_zone_speeds) < SLOWDOWN_DROP_KMPH:
            continue

        visits.append(
            StopVisit(
                stop_name=records[segment[0]].stop_name or "",
                start=records[segment[0]].timestamp,
                end=records[segment[-1]].timestamp,
                record_indexes=tuple(segment),
            )
        )
    return visits


def write_filtered_gps(
    destination: Path, rows: list[dict[str, str]], records: list[GPSRecord], visits: list[StopVisit]
) -> int:
    """Write all rows except observations belonging to detected slowed visits."""
    removed_source_indexes = {
        records[record_index].source_index
        for visit in visits
        for record_index in visit.record_indexes
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=GPS_COLUMNS)
        writer.writeheader()
        for source_index, row in enumerate(rows):
            if source_index not in removed_source_indexes:
                writer.writerow({column: row.get(column, "") for column in GPS_COLUMNS})
    return len(removed_source_indexes)


def safe_filename_component(value: str) -> str:
    """Keep a human-readable vehicle number while preventing path traversal."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" .") or "UNMAPPED"


def format_duration(duration: timedelta) -> str:
    """Format an observed elapsed time as HH:MM:SS."""
    seconds = max(0, round(duration.total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def write_stop_time_files(
    stop_names: list[str],
    vehicle_visits: dict[str, list[StopVisit]],
    vehicle_dates: dict[str, set[str]],
) -> int:
    """Write one stop-by-date matrix for every vehicle with cleaned GPS data."""
    STOP_EFFECT_DIR.mkdir(parents=True, exist_ok=True)
    files_written = 0
    for vehicle_number, visits in sorted(vehicle_visits.items()):
        # Use every date on which the vehicle has a GPS file, including dates
        # without a detected stop slowdown.  Empty cells then mean no such
        # event was observed, rather than an omitted date.
        dates = sorted(vehicle_dates[vehicle_number])
        visits_by_stop_and_date: dict[tuple[str, str], list[str]] = defaultdict(list)
        for visit in visits:
            date = visit.start.date().isoformat()
            visits_by_stop_and_date[(visit.stop_name, date)].append(
                f"{visit.start:%H:%M:%S}-{visit.end:%H:%M:%S} ({format_duration(visit.duration)})"
            )

        destination = STOP_EFFECT_DIR / f"stop_time_{safe_filename_component(vehicle_number)}.csv"
        with destination.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=["BUS STOP", *dates])
            writer.writeheader()
            for stop_name in stop_names:
                writer.writerow(
                    {
                        "BUS STOP": stop_name,
                        **{
                            date: "; ".join(visits_by_stop_and_date[(stop_name, date)])
                            for date in dates
                        },
                    }
                )
        files_written += 1
    return files_written


def main() -> None:
    """Filter stop-slowdown observations and produce vehicle stop-time matrices."""
    if not CLEANED_GPS_DATA_DIR.is_dir():
        raise FileNotFoundError(f"Cleaned GPS directory was not found: {CLEANED_GPS_DATA_DIR}")

    stops = read_stops(STOPS_FILE)
    vehicle_numbers = read_vehicle_numbers(VEHICLES_FILE)
    stop_names = [stop.label for stop in stops]
    vehicle_visits: dict[str, list[StopVisit]] = defaultdict(list)
    vehicle_dates: dict[str, set[str]] = defaultdict(set)
    processed_files = 0
    removed_records = 0

    for source_file in sorted(CLEANED_GPS_DATA_DIR.rglob("*.csv")):
        records, rows = read_gps_records(source_file)
        # Work in timestamp order for visit detection, but preserve the source
        # row order in the filtered CSV.
        records.sort(key=lambda record: record.timestamp)
        assign_nearest_stop(records, stops)
        visits = slowed_stop_visits(records)

        relative_path = source_file.relative_to(CLEANED_GPS_DATA_DIR)
        removed_records += write_filtered_gps(
            FILTERED_GPS_DATA_DIR / relative_path, rows, records, visits
        )
        imei = source_file.stem
        vehicle_number = vehicle_numbers.get(imei) or imei
        vehicle_visits[vehicle_number].extend(visits)
        vehicle_dates[vehicle_number].add(relative_path.parent.name)
        processed_files += 1

    # Include vehicles with no detected slowdown so every processed vehicle has
    # the requested matrix (its date columns will be empty when there are none).
    processed_imeis = {
        source_file.stem for source_file in CLEANED_GPS_DATA_DIR.rglob("*.csv")
    }
    for imei in processed_imeis:
        vehicle_visits.setdefault(vehicle_numbers.get(imei) or imei, [])

    stop_time_files = write_stop_time_files(stop_names, vehicle_visits, vehicle_dates)
    print(
        f"Processed {processed_files:,} cleaned GPS files; removed {removed_records:,} "
        f"GPS observations in {sum(map(len, vehicle_visits.values())):,} slowed "
        f"stop visits. Wrote {stop_time_files:,} stop-time files to {STOP_EFFECT_DIR} "
        f"and filtered GPS data to {FILTERED_GPS_DATA_DIR}."
    )


if __name__ == "__main__":
    main()
