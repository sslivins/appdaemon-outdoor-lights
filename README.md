# appdaemon-outdoor-lights

An [AppDaemon](https://appdaemon.readthedocs.io/) app for Home Assistant that
turns off a set of outdoor lights/switches at a scheduled time each night — but
**defers the shut-off while someone is still outside**, and only kills the
lights once everyone has gone in.

> Personal project, shared as-is. Adapt the entity IDs in
> `outdoor_lights_off.yaml` to your own setup.

## How it works

- At `off_time` (default **22:00**) the app *arms*.
- It will not turn the lights off while **someone is outside**. "Outside" is
  detected by two configurable signals:
  1. **AP presence** — a household member's phone is connected to a specific
     access point, matched by the device_tracker's `ap_mac` attribute (this
     replicates a common Home Assistant template-sensor pattern, in Python).
  2. **Door sensors** — one or more doors are open.
- As soon as nobody is outside (and all watched doors are closed), the lights
  turn off. A short debounce (`clear_delay_seconds`) rides out brief AP
  drop-offs so a phone momentarily losing Wi‑Fi doesn't cause a false shut-off.
- At `reset_time` (default **04:00**) the app **force-offs everything
  unconditionally** as a hard failsafe, then disarms for the day.
- If AppDaemon (re)starts after `off_time` but before `reset_time`, the app
  arms immediately so a restart never skips the night.

No entity IDs are hardcoded in the Python — everything comes from the YAML.

## Installation

1. Copy this folder into your AppDaemon `apps/` directory
   (e.g. `apps/outdoor_lights_off/`).
2. Edit `outdoor_lights_off.yaml` for your own entities (see below).

AppDaemon auto-discovers `outdoor_lights_off.yaml` and loads
`outdoor_lights_off.py`. No extra Python dependencies are required.

## Configuration (`outdoor_lights_off.yaml`)

| Key | What it is |
| --- | --- |
| `off_time` | Time each night to start trying to shut the lights off. |
| `reset_time` | Hard-failsafe time: force everything off, then disarm. May wrap past midnight. |
| `clear_delay_seconds` | Debounce after everyone goes inside before turning off (set `0` to disable). |
| `poll_interval_seconds` | Safety-net re-check interval while armed (AP presence is sampled). |
| `lights` | List of `light.*` / `switch.*` entities to turn off. |
| `presence.door_sensors[]` | `entity` + `open_state`; any open door means someone is outside. |
| `presence.ap_presence.ap_mac` | The access point MAC that means "in the backyard". |
| `presence.ap_presence.person_entities` | `person.*` entities whose device_trackers are checked against `ap_mac`. |

### Example

```yaml
outdoor_lights_off:
  module: outdoor_lights_off
  class: OutdoorLightsOff
  off_time: "22:00:00"
  reset_time: "04:00:00"
  clear_delay_seconds: 120
  poll_interval_seconds: 60
  lights:
    - light.exterior_deck_wall_lights
    - switch.pond_waterfall_pump
  presence:
    door_sensors:
      - entity: binary_sensor.kitchen_double_sliding_doors
        open_state: "on"
    ap_presence:
      ap_mac: "ac:8b:a9:de:1a:68"
      person_entities:
        - person.example
```

### Finding your AP MAC

The `ap_mac` is the MAC the client reports for the access point it's connected
to. With UniFi device trackers you can read it from a known-outside phone:
`{{ state_attr('device_tracker.your_phone', 'ap_mac') }}` in
**Developer Tools → Template**.

## Files

| File | Purpose |
| --- | --- |
| `outdoor_lights_off.py` | The app logic. |
| `outdoor_lights_off.yaml` | App configuration (edit for your setup). |

## License

MIT
