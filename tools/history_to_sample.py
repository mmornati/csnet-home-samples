#!/usr/bin/env python3
"""Rebuild a sample from Home Assistant's history of the CSNet Home entities.

The sample recorder of the integration only records from now on. Past days
can still be shared: Home Assistant keeps the history of every entity
(10 days by default). This tool turns that history back into what CSNet
Manager sent, poll by poll, so it can be replayed like a recorded sample.

It needs a short recorded sample of the same unit as a template: the shape of
the replies and the fields Home Assistant has no entity for are taken from
it. Record 15 minutes with the ``csnet_home.start_sample_recording`` action
first.

    python tools/history_to_sample.py \\
        --ha-url http://homeassistant.local:8123 --token-file ~/.ha-token \\
        --template csnet-sample-20261119-0637.jsonl.gz \\
        --start 2026-11-13T00:00 --end 2026-11-16T00:00 \\
        --meter-energy sensor.my_meter_energy --meter-power sensor.my_meter_power \\
        --out samples/my-unit/2026-11-13.jsonl.gz

A rebuilt sample is marked ``"source": "ha-history"``. Its readings come from
what Home Assistant stored: a value only changes in history when it changes on
the unit, so the result matches what the integration read at each poll. The
fields listed in ``rebuilt_fields`` follow the history; every other field keeps
the template's value for the whole sample.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import gzip
import json
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

FORMAT = "csnet-home-sample"
VERSION = 1
POLL_SECONDS = 60
ENERGY_POLL_SECONDS = 900
ALARM_POLL_SECONDS = 300
DOMAIN = "csnet_home"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(round(number))


def _on(value: Any) -> int | None:
    return {"on": 1, "off": 0}.get(value)


def _times(factor: float) -> Callable[[Any], int | None]:
    def convert(value: Any) -> int | None:
        number = _num(value)
        return None if number is None else int(round(number * factor))

    return convert


# Unit sensors: the key of the entity's unique id -> heatingStatus field and how to get the raw value back
STATUS_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "installation-in_water_temperature": ("waterInletTemp", _int),
    "installation-out_water_temperature": ("waterOutletTemp", _int),
    "installation-heat_exchanger_water_outlet_temperature": ("waterOutletHPTemp", _int),
    "installation-c2_water_temperature": ("waterOutlet2Temp", _int),
    "installation-set_water_temperature": ("waterTempSetting", _int),
    "installation-water_pressure": ("waterPressure", _times(50)),
    "installation-water_flow": ("waterFlow", _times(10)),
    "installation-pump_speed": ("pumpSpeed", _int),
    "installation-mix_valve_position": ("mixingValveOpening", _int),
    "installation-external_temperature": ("outdoorAmbientTemp", _int),
    "installation-mean_external_temperature": ("outdoorAmbientAverageTemp", _int),
    "installation-gas_temperature": ("gasTemp", _int),
    "installation-liquid_temperature": ("liquidTemp", _int),
    "installation-defrost": ("defrosting", _on),
    "compressor-compressor_frequency": ("ouHz", _int),
    "compressor-compressor_current": ("ouCurrent", _int),
    "compressor-compressor_capacity": ("unitCapacity", _int),
    "compressor-discharge_temperature": ("ouDischargeTemperature", _int),
    "compressor-evaporator_temperature": ("ouEvapTemperature", _int),
    "compressor-outdoor_ambient_temperature": ("ouAmbientTemperature", _int),
    "compressor-discharge_pressure": ("ouDischargePress", _int),
    "compressor-suction_pressure": ("ouSuctionPress", _int),
    "compressor-expansion_valve_opening": ("evi", _int),
    "compressor-ou_evo_1": ("ouEvo1", _int),
    "compressor-outdoor_fan_rpm": ("fanRPM", _int),
}
# Sensors whose raw value is an attribute
STATUS_ATTRIBUTE_FIELDS = {
    "compressor-operation_status": ("operationStatus", "raw_value"),
    "compressor-system_status_flags": ("systemStatus2Flags", "raw_value"),
}
# Zone sensors (csnet_home-zone-<device>-<room>-<type>-<key>) -> element field
ZONE_FIELDS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "current_temperature": ("currentTemperature", _num),
    "setting_temperature": ("settingTemperature", _num),
    "on_off": ("onOff", _on),
    "mode": ("mode", lambda v: {"cool": 0, "heat": 1, "heat_cool": 2}.get(v)),
    "doingBoost": ("doingBoost", lambda v: {"on": True, "off": False}.get(v)),
    "alarm_code": ("alarmCode", _int),
}
# Climate attributes -> element field
CLIMATE_ATTRIBUTES = {
    "c1_demand": "c1Demand",
    "c2_demand": "c2Demand",
    "operation_status": "operationStatus",
    "real_mode": "realMode",
    "timer_running": "timerRunning",
}


class Series:
    """The history of one entity: its states and attributes over time."""

    def __init__(self, rows: list[dict]) -> None:
        self.times: list[float] = []
        self.states: list[Any] = []
        self.attributes: list[dict] = []
        attributes: dict = {}
        for row in rows:
            if "a" in row:
                attributes = row["a"]
            self.times.append(row.get("lu") or row["lc"])
            self.states.append(row["s"])
            self.attributes.append(attributes)

    def at(self, when: float) -> tuple[Any, dict]:
        """Return the state and attributes at a time (None before the first one)."""
        index = bisect_right(self.times, when) - 1
        if index < 0:
            return None, {}
        state = self.states[index]
        return (None if state in ("unknown", "unavailable") else state), self.attributes[index]


def read_template(path: Path) -> dict[str, Any]:
    """Return the last reply of each kind in a recorded sample."""
    replies: dict[str, Any] = {}
    with gzip.open(path, "rt", encoding="utf-8") as file:
        header = json.loads(file.readline())
        if header.get("format") != FORMAT:
            raise SystemExit(f"{path} is not a sample")
        for line in file:
            if line.strip():
                item = json.loads(line)
                if item["kind"] != "meter":
                    replies[item["kind"]] = item["data"]
    for kind in ("elements", "installation_devices"):
        if kind not in replies:
            raise SystemExit(f"The template has no {kind} reply: record a few minutes more")
    replies["_header"] = header
    return _scrub(replies)


# Identifiers some older recordings did not redact
_SCRUB_KEYS = {"installation_id"}


def _scrub(data: Any) -> Any:
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if k in _SCRUB_KEYS and v not in (None, "", 0):
                out[k] = "**REDACTED**"
            elif k == "firmware" and isinstance(v, str) and len(v.split()) > 1 and v.split()[1] != "**REDACTED**":
                # the version is kept, the module's number is not
                out[k] = f"{v.split()[0]} **REDACTED**"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(data, list):
        return [_scrub(item) for item in data]
    return data


async def fetch_history(url: str, token: str, start: datetime, end: datetime, extra: list[str]) -> tuple[list[dict], dict]:
    """Return the integration's entities and their history from Home Assistant's websocket API."""
    import aiohttp  # only needed to fetch

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"{url.rstrip('/')}/api/websocket", max_msg_size=0) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            if (await ws.receive_json()).get("type") != "auth_ok":
                raise SystemExit("Home Assistant refused the token")
            await ws.send_json({"id": 1, "type": "config/entity_registry/list"})
            registry = [e for e in (await ws.receive_json())["result"] if e.get("platform") == DOMAIN]
            entity_ids = [e["entity_id"] for e in registry] + extra
            await ws.send_json(
                {
                    "id": 2,
                    "type": "history/history_during_period",
                    # the state at the start is needed too
                    "start_time": (start - timedelta(hours=1)).isoformat(),
                    "end_time": end.isoformat(),
                    "entity_ids": entity_ids,
                    "minimal_response": False,
                    "no_attributes": False,
                    "significant_changes_only": False,
                }
            )
            history = (await ws.receive_json())["result"]
    return registry, history


def _record_date(record: dict) -> tuple[int, int, int]:
    time = record.get("time") or {}
    return (time.get("year") or 0, time.get("month") or 0, time.get("day") or 0)


def _summed(records: list[dict], template: dict, uses_from: list[dict]) -> dict:
    """Return a record like template whose amounts are the sums of records."""
    out = copy.deepcopy(template)
    for part in ("input", "capacity"):
        if isinstance(out.get(part), dict):
            out[part] = {use: sum(((r.get(part) or {}).get(use) or 0) for r in records) for use in out[part]}
    if uses_from:
        out["time"] = copy.deepcopy(uses_from[-1]["time"])
    return out


def _record_local(record: dict) -> datetime | None:
    """Return when a record was written, in the unit's local time (naive).

    The record's "timestamp" is that local time read as if it were UTC, so it
    is off by the time zone's offset: the date and time fields are used.
    """
    time = record.get("time") or {}
    try:
        return datetime(time["year"], time["month"], time["day"], time.get("hour") or 0, time.get("minute") or 0, time.get("second") or 0)
    except (KeyError, TypeError, ValueError):
        return None


def energy_at(reply: Any, until_ms: float, local: datetime) -> Any:  # noqa: ARG001 - until_ms kept for callers
    """Return the energy records reply as it was at a time.

    CSNet stamps the record of a past day or month when it was created, but it
    holds the whole period's total; the record of the current day and month
    is stamped with its last update. So: the hours written by then are kept,
    earlier days and months are kept as they are, and the current day's and
    month's totals are summed again from what was written by then.
    """
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        return reply
    today = (local.year, local.month, local.day)
    out = copy.deepcopy(reply)
    for unit in out["data"]:
        now = local.replace(tzinfo=None)
        hourly = [r for r in unit.get("hourly") or [] if (_record_local(r) or now) <= now]
        hourly.sort(key=lambda r: _record_local(r) or now)
        unit["hourly"] = hourly
        daily_all = unit.get("daily") or []
        daily = [r for r in daily_all if _record_date(r) < today]
        hours_today = [r for r in hourly if _record_date(r) == today]
        if hours_today and daily_all:
            daily.append(_summed(hours_today, daily_all[-1], hours_today))
        unit["daily"] = daily
        monthly_all = unit.get("monthly") or []
        monthly = [r for r in monthly_all if _record_date(r)[:2] < today[:2]]
        days_this_month = [r for r in daily if _record_date(r)[:2] == today[:2]]
        if days_this_month and monthly_all:
            monthly.append(_summed(days_this_month, monthly_all[-1], days_this_month))
        unit["monthly"] = monthly
    return out


def _filter_alarms(reply: Any, local: datetime) -> Any:
    """Return the alarm list without the alarms raised after a time (createdAtString is local time)."""
    if not isinstance(reply, dict) or not isinstance(reply.get("data"), list):
        return reply
    now = local.replace(tzinfo=None)
    out = dict(reply)
    kept = []
    for alarm in reply["data"]:
        try:
            created = datetime.fromisoformat(alarm.get("createdAtString") or "")
        except ValueError:
            created = None
        if created is None or created <= now:
            kept.append(alarm)
    out["data"] = kept
    return out


def _custom_timestamp(local: datetime) -> dict:
    return {
        "time": int(local.strftime("%Y%m%d%H%M%S")),
        "year": local.year,
        "month": local.month,
        "day": local.day,
        "hour": local.hour,
        "minute": local.minute,
        "second": local.second,
        # CSNet writes the local time as if it were UTC
        "timestamp": int(local.replace(tzinfo=timezone.utc).timestamp() * 1000),
        "asSQLCustomTime": int(local.strftime("%Y%m%d%H%M%S")),
    }


def build(
    template: dict,
    registry: list[dict],
    history: dict,
    start: datetime,
    end: datetime,
    time_zone: str,
    meter_energy: str | None,
    meter_power: str | None,
    notes: str,
) -> tuple[dict, list[dict]]:
    """Return the header and the lines of the rebuilt sample."""
    tz = ZoneInfo(time_zone)
    by_key = {e["unique_id"].removeprefix(f"{DOMAIN}-"): e["entity_id"] for e in registry}
    series = {entity: Series(rows) for entity, rows in history.items()}

    status_sources = {field: (series[by_key[key]], convert) for key, (field, convert) in STATUS_FIELDS.items() if by_key.get(key) in series}
    attribute_sources = {field: (series[by_key[key]], attr) for key, (field, attr) in STATUS_ATTRIBUTE_FIELDS.items() if by_key.get(key) in series}
    weather = series.get(by_key.get("installation-weather_temperature", ""))

    zones: dict[tuple, dict] = {}
    for key, entity_id in by_key.items():
        if entity_id not in series:
            continue
        parts = key.split("-")
        if parts[0] == "zone" and len(parts) == 5:
            device, room, element_type, name = int(parts[1]), int(parts[2]), int(parts[3]), parts[4]
            zone = zones.setdefault((device, room, element_type), {"fields": {}, "climate": None, "rssi": None, "last_comm": None})
            if name in ZONE_FIELDS:
                zone["fields"][ZONE_FIELDS[name][0]] = (series[entity_id], ZONE_FIELDS[name][1])
            elif name == "wifi_signal":
                zone["rssi"] = series[entity_id]
            elif name == "last_communication":
                zone["last_comm"] = series[entity_id]
        elif parts[0] == "climate" and len(parts) == 4:
            zones.setdefault((int(parts[1]), int(parts[2]), int(parts[3])), {"fields": {}, "climate": None, "rssi": None, "last_comm": None})["climate"] = series[entity_id]

    rebuilt = sorted(set(status_sources) | set(attribute_sources) | {f for z in zones.values() for f in z["fields"]})
    header = {
        "format": FORMAT,
        "version": VERSION,
        "source": "ha-history",
        "start": start.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "time_zone": time_zone,
        "integration_version": None,
        "scan_interval": POLL_SECONDS,
        "power_supply": template["_header"].get("power_supply"),
        "meter": {"energy": bool(meter_energy), "power": bool(meter_power)},
        "notes": notes,
        "template_integration_version": template["_header"].get("integration_version"),
        "rebuilt_fields": rebuilt,
    }

    # a reply of the slower kinds is written only when it changed: the replay keeps the last one
    last_written: dict[str, str] = {}

    def changed(kind: str, data: Any) -> bool:
        text = json.dumps(data, sort_keys=True)
        if last_written.get(kind) == text:
            return False
        last_written[kind] = text
        return True

    energy_meter = series.get(meter_energy) if meter_energy else None
    power_meter = series.get(meter_power) if meter_power else None
    lines: list[dict] = []
    total = int((end - start).total_seconds())
    for offset in range(0, total, POLL_SECONDS):
        when = start + timedelta(seconds=offset)
        epoch = when.timestamp()
        ms = epoch * 1000
        local = when.astimezone(tz)

        elements = copy.deepcopy(template["elements"])
        data = elements.get("data", {})
        if weather is not None:
            value = _num(weather.at(epoch)[0])
            if value is not None:
                data["weatherTemperature"] = value
        zone_by_id = {}
        for element in data.get("elements", []):
            zone = zones.get((element.get("deviceId"), element.get("roomId"), element.get("elementType")))
            if not zone:
                continue
            zone_by_id[element.get("elementType")] = element
            for field, (source, convert) in zone["fields"].items():
                value = convert(source.at(epoch)[0])
                if value is not None:
                    if field == "settingTemperature" and element.get("elementType") == 5:
                        value = value / 10
                    element[field] = value
            if zone["climate"] is not None:
                _, attributes = zone["climate"].at(epoch)
                for attribute, field in CLIMATE_ATTRIBUTES.items():
                    if attribute in attributes and attributes[attribute] is not None:
                        element[field] = attributes[attribute]
            for device in data.get("device_status", []):
                if device.get("id") == element.get("deviceId"):
                    device["currentTimeMillis"] = int(ms)
                    if zone["rssi"] is not None and _int(zone["rssi"].at(epoch)[0]) is not None:
                        device["rssi"] = _int(zone["rssi"].at(epoch)[0])
                    last = zone["last_comm"].at(epoch)[0] if zone["last_comm"] is not None else None
                    if last:
                        device["lastComm"] = int(datetime.fromisoformat(last).timestamp() * 1000)
        lines.append({"t": offset, "kind": "elements", "data": elements})

        devices = copy.deepcopy(template["installation_devices"])
        for device in devices.get("data", []):
            device["customTimestamp"] = _custom_timestamp(local)
            for indoor in device.get("indoors", []):
                status = indoor.get("heatingStatus")
                if not isinstance(status, dict):
                    continue
                for field, (source, convert) in status_sources.items():
                    value = convert(source.at(epoch)[0])
                    if value is not None:
                        status[field] = value
                for field, (source, attribute) in attribute_sources.items():
                    value = source.at(epoch)[1].get(attribute)
                    if value is not None:
                        status[field] = value
                setting = indoor.get("heatingSetting") if isinstance(indoor.get("heatingSetting"), dict) else {}
                for number in (1, 2):
                    element = zone_by_id.get(number)
                    if not element:
                        continue
                    for target, field in ((status, "ambientTempRoomThermostatZ"), (setting, "ambientTempRoomZ")):
                        if element.get("currentTemperature") is not None and f"{field}{number}" in target:
                            target[f"{field}{number}"] = int(round(element["currentTemperature"] * 10))
                    for target, field in ((status, "settingTempRoomThermostatZ"), (setting, "settingTempRoomZ")):
                        if element.get("settingTemperature") is not None and f"{field}{number}" in target:
                            target[f"{field}{number}"] = int(round(element["settingTemperature"] * 10))
        lines.append({"t": offset, "kind": "installation_devices", "data": devices})

        if energy_meter is not None or power_meter is not None:
            meter: dict = {"t": offset, "kind": "meter"}
            if energy_meter is not None:
                meter["energy_kwh"] = _num(energy_meter.at(epoch)[0])
            if power_meter is not None:
                meter["power_w"] = _num(power_meter.at(epoch)[0])
            lines.append(meter)
        if offset % ALARM_POLL_SECONDS == 0 and "installation_alarms" in template:
            alarms = _filter_alarms(template["installation_alarms"], local)
            if changed("installation_alarms", alarms):
                lines.append({"t": offset, "kind": "installation_alarms", "data": alarms})
        if offset % ENERGY_POLL_SECONDS == 0 and "energy_records" in template:
            energy = energy_at(template["energy_records"], ms, local)
            if changed("energy_records", energy):
                lines.append({"t": offset, "kind": "energy_records", "data": energy})
    return header, lines


def write(path: Path, header: dict, lines: list[dict]) -> None:
    """Write a sample file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as file:
        file.write(json.dumps(header, separators=(",", ":"), ensure_ascii=False) + "\n")
        for line in lines:
            file.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--template", type=Path, required=True, help="a recorded sample of the same unit")
    parser.add_argument("--start", required=True, help="local start, e.g. 2026-11-13T00:00")
    parser.add_argument("--end", required=True, help="local end (excluded)")
    parser.add_argument("--time-zone", default=None, help="Home Assistant's time zone (default: the template's)")
    parser.add_argument("--ha-url", help="Home Assistant URL, to fetch the history")
    parser.add_argument("--token-file", type=Path, help="file holding a long-lived access token")
    parser.add_argument("--registry", type=Path, help="saved entity registry list (instead of fetching)")
    parser.add_argument("--history", type=Path, help="saved history_during_period result (instead of fetching)")
    parser.add_argument("--meter-energy", help="energy sensor of a meter on the heat pump's circuit")
    parser.add_argument("--meter-power", help="power sensor of the same meter")
    parser.add_argument("--notes", default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    template = read_template(args.template)
    time_zone = args.time_zone or template["_header"].get("time_zone") or "UTC"
    tz = ZoneInfo(time_zone)
    start = datetime.fromisoformat(args.start).replace(tzinfo=tz)
    end = datetime.fromisoformat(args.end).replace(tzinfo=tz)
    extra = [e for e in (args.meter_energy, args.meter_power) if e]
    if args.history and args.registry:
        registry = json.loads(args.registry.read_text(encoding="utf-8"))
        history = json.loads(args.history.read_text(encoding="utf-8"))
    elif args.ha_url and args.token_file:
        token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
        registry, history = asyncio.run(fetch_history(args.ha_url, token, start, end, extra))
    else:
        parser.error("give --ha-url and --token-file, or --registry and --history")
    header, lines = build(template, registry, history, start, end, time_zone, args.meter_energy, args.meter_power, args.notes)
    write(args.out, header, lines)
    print(f"{args.out}: {sum(1 for line in lines if line['kind'] == 'installation_devices')} polls, {args.out.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
