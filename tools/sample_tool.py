#!/usr/bin/env python3
"""Check the samples and build the catalog.

    python tools/sample_tool.py validate [files...]   # every sample when no file is given
    python tools/sample_tool.py index                 # writes site/index.json
    python tools/sample_tool.py stats <file>          # prints what a sample covers

Only the standard library is used.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SITE = ROOT / "site"
FORMAT = "csnet-home-sample"
VERSIONS = {1}
KINDS = {"elements", "installation_devices", "installation_alarms", "energy_records", "installations", "meter"}
MAX_BYTES = 25 * 1024 * 1024
REDACTED = "**REDACTED**"
RAW_BASE = "https://raw.githubusercontent.com/mmornati/csnet-home-samples/main/"

# Keys that must never carry a value in a shared sample
SECRET_KEYS = {"latitude", "longitude", "ownerid", "installationid", "installation_id", "installation", "administrator", "username", "password", "token", "_csrf", "user", "address", "city", "zipcode", "postalcode", "phone", "hash", "key", "mac", "ip", "ssid"}
SECRET_FRAGMENTS = ("serial", "mail", "owner", "displayablename", "password", "token", "macaddr", "ipaddr")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
UNIT_FIELDS = {"name", "model", "capacity_hp", "power_supply", "suction_sensor", "dhw", "zones", "meter", "country"}
SUCTION_PRESSURE_ABSENT = 128
MISSING_PROBE = (-127, -67, 255, 127)


def read(path: Path) -> tuple[dict, list[dict]]:
    with gzip.open(path, "rt", encoding="utf-8") as file:
        header = json.loads(file.readline())
        lines = [json.loads(line) for line in file if line.strip()]
    return header, lines


def _secrets(value: Any, where: str = "") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if (lowered in SECRET_KEYS or any(f in lowered for f in SECRET_FRAGMENTS)) and item not in (None, "", 0, REDACTED, -1, False):
                found.append(f"{where}.{key}")
            else:
                found += _secrets(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _secrets(item, f"{where}[{index}]")
    elif isinstance(value, str) and where.endswith(".firmware") and len(value.split()) > 1 and value.split()[1] != REDACTED:
        found.append(f"{where} (the module's number after the version)")
    elif isinstance(value, str) and EMAIL.search(value):
        found.append(f"{where} (e-mail address)")
    return found


def validate(path: Path) -> list[str]:
    """Return what is wrong with a sample file."""
    problems = []
    if path.stat().st_size > MAX_BYTES:
        problems.append(f"larger than {MAX_BYTES // 1024 // 1024} MB: split it in shorter periods")
    try:
        header, lines = read(path)
    except (OSError, ValueError) as err:
        return [f"not a readable gzip JSON Lines file: {err}"]
    if header.get("format") != FORMAT or header.get("version") not in VERSIONS:
        return ["the first line is not a csnet-home-sample header"]
    for field in ("start", "time_zone", "source"):
        if not header.get(field):
            problems.append(f"the header has no {field}")
    try:
        ZoneInfo(header.get("time_zone") or "UTC")
        datetime.fromisoformat(header.get("start", ""))
    except (ValueError, KeyError) as err:
        problems.append(f"bad start or time zone: {err}")
    last = -1.0
    kinds = defaultdict(int)
    leaks: dict[str, int] = {}
    for number, line in enumerate(lines, start=2):
        kind = line.get("kind")
        if kind not in KINDS:
            problems.append(f"line {number}: unknown kind {kind!r}")
            continue
        if not isinstance(line.get("t"), (int, float)) or line["t"] < last - 30:
            problems.append(f"line {number}: time missing or going back")
        last = max(last, float(line.get("t") or 0))
        kinds[kind] += 1
        if kind == "elements":
            data = (line.get("data") or {}).get("data") or {}
            if data.get("name") not in (None, "", REDACTED):
                leaks.setdefault("the installation's name (elements.data.name)", number)
        for leak in _secrets(line.get("data"), kind):
            # one line per field: the same field repeats in every reply
            leaks.setdefault(re.sub(r"\[\d+\]", "[]", leak), number)
    if not kinds["installation_devices"]:
        problems.append("no installation_devices reply: nothing to replay")
    if not kinds["elements"]:
        problems.append("no elements reply")
    for leak, number in sorted(leaks.items(), key=lambda item: item[1]):
        problems.append(f"not redacted: {leak} (first on line {number})")
    return problems


def _status(reply: Any) -> dict:
    try:
        return reply["data"][0]["indoors"][0]["heatingStatus"] or {}
    except (KeyError, IndexError, TypeError):
        return {}


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name


def stats(path: Path) -> dict[str, Any]:
    """Return what a sample covers, for the catalog."""
    header, lines = read(path)
    tz = ZoneInfo(header.get("time_zone") or "UTC")
    start = datetime.fromisoformat(header["start"])
    polls = [line for line in lines if line["kind"] == "installation_devices"]
    meters = [line for line in lines if line["kind"] == "meter" and line.get("energy_kwh") is not None]
    statuses = [(_status(p["data"]), p["t"]) for p in polls]
    running = starts = defrosts = 0
    previous_hz = previous_defrost = 0
    max_hz = 0
    outdoor = []
    for status, _ in statuses:
        hz = status.get("ouHz") or 0
        running += hz > 0
        starts += hz > 0 and previous_hz == 0
        defrost = status.get("defrosting") or 0
        defrosts += defrost == 1 and previous_defrost != 1
        previous_hz, previous_defrost = hz, defrost
        max_hz = max(max_hz, hz)
        temperature = status.get("outdoorAmbientTemp")
        if isinstance(temperature, (int, float)) and temperature not in MISSING_PROBE:
            outdoor.append(temperature if temperature < 128 else temperature - 256)
    interval = header.get("scan_interval") or 60
    end = start + timedelta(seconds=polls[-1]["t"] if polls else 0)
    first = statuses[0][0] if statuses else {}
    mode_codes = {s.get("mode") for s, _ in statuses}
    heat = None
    energy = [line for line in lines if line["kind"] == "energy_records"]
    if energy:
        # CSNet groups its hourly records by the date in their time, as its daily records do
        first_day, last_day = start.astimezone(tz).date(), end.astimezone(tz).date()
        # record times are the unit's local time
        end_local = (end + timedelta(seconds=interval)).astimezone(tz).replace(tzinfo=None)
        total, counted = 0.0, 0
        for unit in (energy[-1].get("data") or {}).get("data") or []:
            for record in unit.get("hourly") or []:
                time = record.get("time") or {}
                try:
                    written = datetime(time["year"], time["month"], time["day"], time.get("hour") or 0, time.get("minute") or 0)
                except (KeyError, TypeError, ValueError):
                    continue
                day = written.date()
                if first_day <= day <= last_day and written <= end_local and (day > first_day or start.astimezone(tz).hour == 0):
                    total += sum((record.get("capacity") or {}).values())
                    counted += 1
        heat = round(total, 1) if counted else None
    elements = next((line["data"] for line in lines if line["kind"] == "elements"), {})
    zones = len(((elements or {}).get("data") or {}).get("elements") or [])
    return {
        "file": _rel(path),
        "unit": path.parent.name,
        "source": header.get("source"),
        "start": start.astimezone(tz).isoformat(timespec="minutes"),
        "end": end.astimezone(tz).isoformat(timespec="minutes"),
        "days": round((end - start).total_seconds() / 86400, 2),
        "polls": len(polls),
        "scan_interval": interval,
        "integration_version": header.get("integration_version"),
        "running_hours": round(running * interval / 3600, 1),
        "compressor_starts": starts,
        "max_hz": max_hz,
        "defrosts": defrosts,
        "outdoor_min": min(outdoor) if outdoor else None,
        "outdoor_mean": round(sum(outdoor) / len(outdoor), 1) if outdoor else None,
        "outdoor_max": max(outdoor) if outdoor else None,
        "modes": sorted({"cool" if m == 0 else "heat" if m == 1 else "auto" for m in mode_codes if m in (0, 1, 2)}),
        "meter_kwh": round(meters[-1]["energy_kwh"] - meters[0]["energy_kwh"], 2) if len(meters) > 1 else None,
        "meter_power": bool((header.get("meter") or {}).get("power")),
        "csnet_heat_kwh": heat,
        "unit_model_code": first.get("unitModel"),
        "ou_capacity_code": first.get("ouCapacityCode"),
        "suction_sensor": first.get("ouSuctionPress") not in (None, SUCTION_PRESSURE_ABSENT),
        "dhw_probe": first.get("tempDHW") not in (None, *MISSING_PROBE),
        "zones": zones,
        "notes": header.get("notes") or "",
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "url": RAW_BASE + _rel(path),
    }


def units() -> dict[str, dict]:
    found = {}
    for unit_file in sorted(SAMPLES.glob("*/unit.json")):
        found[unit_file.parent.name] = json.loads(unit_file.read_text(encoding="utf-8"))
    return found


def all_samples() -> list[Path]:
    return sorted(SAMPLES.glob("*/*.jsonl.gz"))


UNIT_TYPES = {
    "name": str,
    "model": str,
    "capacity_hp": (int, float),
    "power_supply": str,
    "suction_sensor": bool,
    "dhw": bool,
    "zones": int,
    "meter": str,
    "country": str,
    "contributor": str,
    "notes": str,
}
POWER_SUPPLIES = {"single_phase", "three_phase", "unknown"}


def validate_unit(data: Any) -> list[str]:
    """Return what is wrong with a unit.json."""
    if not isinstance(data, dict):
        return ["not a JSON object"]
    problems = [f"missing {field}" for field in sorted(UNIT_FIELDS - set(data) - {"country"})]
    for field, value in data.items():
        expected = UNIT_TYPES.get(field)
        if expected is None:
            problems.append(f"unknown field {field}")
        elif not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            problems.append(f"{field} has the wrong type")
    if data.get("power_supply") not in POWER_SUPPLIES:
        problems.append(f"power_supply must be one of {', '.join(sorted(POWER_SUPPLIES))}")
    return problems


def cmd_validate(files: list[str]) -> int:
    paths = [Path(f) for f in files] or all_samples()
    failed = False
    for unit, data in units().items():
        for problem in validate_unit(data):
            print(f"samples/{unit}/unit.json: {problem}")
            failed = True
    for path in paths:
        if path.name.endswith(".jsonl.gz") and not (path.parent / "unit.json").exists():
            print(f"{path}: the folder has no unit.json describing the unit")
            failed = True
        problems = validate(path)
        print(f"{path}: {'OK' if not problems else 'FAILED'}")
        for problem in problems:
            print(f"  - {problem}")
        failed |= bool(problems)
    return 1 if failed else 0


def cmd_index() -> int:
    unit_data = units()
    samples = []
    for path in all_samples():
        item = stats(path)
        item["unit_info"] = unit_data.get(path.parent.name, {})
        samples.append(item)
    SITE.mkdir(exist_ok=True)
    index = {"format": FORMAT, "generated": datetime.now().astimezone().isoformat(timespec="seconds"), "samples": samples}
    (SITE / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"site/index.json: {len(samples)} samples")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"validate", "index", "stats"}:
        print(__doc__)
        return 2
    if argv[1] == "validate":
        return cmd_validate(argv[2:])
    if argv[1] == "index":
        return cmd_index()
    for name in argv[2:]:
        print(json.dumps(stats(Path(name).resolve()), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
