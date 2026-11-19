# CSNet Home samples

Recorded data of real Hitachi heat pumps: what CSNet Manager sent, poll by poll,
with the reading of a meter when the owner has one. Developers of the
[CSNet Home integration](https://github.com/mmornati/home-assistant-csnet-home)
replay them to see what a change would show on each unit before it ships.

**Catalog:** https://mmornati.github.io/csnet-home-samples/ (filter by unit,
power supply, meter, defrosts; download links).

Not affiliated with Hitachi.

## Use them

From a checkout of the integration:

```bash
python scripts/fetch_samples.py --list     # what is available
python scripts/fetch_samples.py --meter    # or --all, --unit, --power-supply, --defrost
pytest -m replay
```

The files go to `tests/samples/` (ignored by git). Each sample is set up as a
config entry, its replies are fed to the API client at the recorded times, and
the replay fails on an error logged by the integration or on an impossible
figure (negative power, a COP out of range, a daily total going down).

To see what a change does to the figures, write a report on both branches and
compare them:

```bash
CSNET_REPLAY_REPORT=head.json pytest -m replay
git switch main
CSNET_REPLAY_REPORT=base.json pytest -m replay
python -m tests.replay.report base.json head.json
```

This prints, for each complete day, the estimated electricity, heat and COP
before → after, next to the meter's kWh and CSNet's own heat.

## Share your unit

1. In Home Assistant, run the action **CSNet Home: Start sample recording**.
   Two days or more cover the daily totals and CSNet's hourly heat records. A
   day with defrosts, a DHW cycle or cooling is worth more than a longer quiet
   period. If you have a meter on the heat pump's circuit, pick its power
   sensor too (its energy sensor is taken from the integration's settings).
2. When it ends, Home Assistant shows where the file is: `csnet_home_samples/`
   in the configuration folder.
3. Open a [Share a sample](https://github.com/mmornati/csnet-home-samples/issues/new?template=share-sample.yml)
   issue and attach it, or open a pull request adding it under
   `samples/<your-unit>/` with a `unit.json` (see the existing ones).

The validation workflow checks every pull request: the format, the header, and
that nothing private is left in the replies.

### Past days, from Home Assistant's history

Home Assistant keeps the history of every entity for 10 days by default.
`tools/history_to_sample.py` rebuilds what CSNet sent from it, using a short
recorded sample of the same unit (15 minutes is enough) as a template for the
shape of the replies. Such samples are marked *rebuilt from history* in the
catalog; their header lists the fields that follow the history (`rebuilt_fields`),
every other field keeps the template's value.

How close is a rebuilt sample to a recording? On 19 Nov a second Home Assistant
recorded the same unit while the main one stored its history. Once the 1–2
minutes between the two clients' polls are accounted for, 90 % of the rebuilt
readings equal the recorded ones; the rest are values that changed between the
two polls. A rebuilt sample is what the integration of the main Home Assistant
read, at its own poll times.

```bash
python tools/history_to_sample.py --ha-url http://homeassistant.local:8123 --token-file ~/.ha-token \
  --template csnet-sample-20261119-0637.jsonl.gz --start 2026-11-13T00:00 --end 2026-11-16T00:00 \
  --meter-energy sensor.my_meter_energy --meter-power sensor.my_meter_power \
  --out samples/my-unit/2026-11-13-cold.jsonl.gz
```

## Privacy

A sample is redacted the same way as the integration's diagnostics download:

- **Removed:** the installation's name, its position (latitude, longitude), the
  account (user, owner, e-mail, administrator), serial numbers, keys, hashes and
  network identifiers. Only the number of installations is kept.
- **Kept:** zone and device names (the entities are named after them), the
  readings and settings of the unit, the alarm history, CSNet's energy records,
  and the **dates and times** of the recording: they say when the heat pump ran
  and when the rooms were heated, which can tell when a home is occupied.

Look at your file before sharing it if that matters to you
(`gunzip -c file.jsonl.gz | less`). Samples are published under
[CC0](LICENSE) (public domain).

## File format

A gzip-compressed JSON Lines file. The first line is a header:

```json
{"format": "csnet-home-sample", "version": 1, "source": "recorder", "start": "2026-11-19T06:37:24+00:00",
 "time_zone": "Europe/Paris", "integration_version": "2.16.0", "scan_interval": 60, "power_supply": "auto",
 "meter": {"energy": true, "power": true}, "notes": ""}
```

Every other line is one reply of CSNet Manager, `t` seconds after the start, or
a meter reading taken right after the readings of the same poll:

```json
{"t": 61.2, "kind": "elements", "data": {...}}
{"t": 61.4, "kind": "installation_devices", "data": {...}}
{"t": 61.4, "kind": "meter", "energy_kwh": 1234.5, "power_w": 1830.0}
{"t": 900.3, "kind": "energy_records", "data": {...}}
```

Kinds: `elements`, `installation_devices`, `installation_alarms`,
`energy_records`, `installations`, `meter`.

## Layout

```
samples/<unit>/unit.json           what the unit is (model, power supply, meter, DHW, zones)
samples/<unit>/<period>.jsonl.gz   the samples
tools/sample_tool.py               validate, stats, build site/index.json
tools/history_to_sample.py         rebuild a sample from Home Assistant's history
site/                              the catalog page (GitHub Pages)
```

## License

Samples: [CC0 1.0](LICENSE). Tools and site: Apache 2.0, like the integration ([LICENSE-CODE](LICENSE-CODE)).
