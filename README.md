<image src="https://user-images.githubusercontent.com/47839015/218900217-81f88955-67b8-4ed8-8e97-271de66c555e.png" align="right" style="margin-top:-2em;width:177px;margin-right:2em;display:inline-block;float:right;"></image>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration) 

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cjkas&repository=ESPSomfy-RTS-HA&category=integration)


# ESPSomfy RTS-HA 
Control up to 32 Somfy shades from Home Assistant, and set their positions, with an ESP32 and an inexpensive CC1101 transceiver. You can also control up to 16 groups, to move several shades at once.

# Requirements
You need an [ESPSomfy RTS](https://github.com/cjkas/ESPSomfy-RTS) device, an ESP32 with a CC1101 transceiver, set up for your shades. The [ESPSomfy RTS wiki](https://github.com/cjkas/ESPSomfy-RTS/wiki) describes how to build and configure one.

| ESPSomfy RTS firmware | Integration version |
|---|---|
| 4.0.0 and later | 3.0.0 and later |
| before 4.0.0 | any; 3.0.0 and later work too |

Firmware 4.0.0 replaced the HTTP API on port 8081 and the socket on port 8080 with one socket on port 80 (protocol 2). Versions of this integration before 3.0.0 can't connect to it. From 3.0.0 the integration speaks protocol 2, and falls back to the old API for older firmware, so update the integration before you update the firmware.

# Installation
Install it with HACS as a [custom repository](https://hacs.xyz/docs/faq/custom_repositories/): add `https://github.com/cjkas/ESPSomfy-RTS-HA` with the type **Integration**, or use the button above. Then restart Home Assistant.

You can also install it by hand: copy `custom_components/espsomfy_rts` into the `config/custom_components` directory of your Home Assistant, and restart it.

If you installed rstrouse's version before, remove it from HACS before adding this one: both install as `espsomfy_rts`, so only one can be installed at a time.

## Setup
Home Assistant finds ESPSomfy RTS devices on your network on its own (over mDNS). They show up under Settings &rarr; Devices & Services, ready to configure. To add one it hasn't found, choose **Add Integration**, pick ESPSomfy RTS, and enter the device's IP address or hostname.

If the device's security is set to a PIN or a username and password, Home Assistant asks for them. When the device stops accepting them, for instance after you change them there, Home Assistant asks for them again under Settings &rarr; Devices & Services.

# Functionality
Once configured you can open, close and set the position of your shades from Home Assistant. The integration follows each shade's position however it was moved, including from a Telis remote or the ESPSomfy RTS web page. Shades can be added to your dashboards and automated with services.

![image](https://user-images.githubusercontent.com/47839015/213933858-95042e9e-0874-4e58-8123-87146439a20e.png)

Each ESPSomfy RTS device gets:
* a **cover** for each shade and each group, with tilt where the shade has it;
* a **switch** for each dry contact shade;
* for shades and groups with a sun sensor, a **switch** to turn it on and off, and **binary sensors** for sunny and windy, and the windy one for awnings without a sun sensor;
* **buttons** to reboot the device and to save a backup;
* diagnostic **sensors**: IP address, WiFi signal and network, free memory, and more;
* a **firmware** entity that shows the installed version.

## Firmware updates
Since firmware 4.0.0 the firmware entity only shows the installed version: the device no longer downloads updates itself. To update, download the release from [GitHub](https://github.com/cjkas/ESPSomfy-RTS/releases) and upload it on the device's web page, as described in [Updating ESPSomfy RTS](https://github.com/cjkas/ESPSomfy-RTS/wiki/Updating-ESPSomfy-RTS).

HACS tells you when there is a new version of this integration.

# Services
All the services act on ESPSomfy RTS cover entities, except `reboot` and `backup`, which act on the device's buttons.

| Service | Fields | Does |
|---|---|---|
| `espsomfy_rts.open_shade` | | opens the shade fully |
| `espsomfy_rts.close_shade` | | closes the shade fully |
| `espsomfy_rts.stop_shade` | | stops the shade where it is |
| `espsomfy_rts.set_shade_position` | `position` 0-100 | moves the shade to this % open |
| `espsomfy_rts.tilt_open` | | opens the slats |
| `espsomfy_rts.tilt_close` | | closes the slats |
| `espsomfy_rts.set_tilt_position` | `tilt_position` 0-100 | moves the slats to this % open |
| `espsomfy_rts.set_current_position` | `position` 0-100 | sets where the shade is thought to be, without moving it. Handy when another sensor knows the real position. |
| `espsomfy_rts.set_current_tilt_position` | `tilt_position` 0-100 | the same for the slats |
| `espsomfy_rts.set_sunny` | `sunny` true/false | tells the motor whether it is sunny, as a Somfy sun sensor would |
| `espsomfy_rts.set_windy` | `windy` true/false | tells the motor whether it is windy, as a Somfy wind sensor would |
| `espsomfy_rts.send_command` | `command`, optional `repeat` 0-50 | sends any RTS command: `Up`, `My`, `Down`, `Toggle`, `Prog`, `UpDown`, `MyUp`, `MyDown`, `MyUpDown`, `StepUp`, `StepDown`, `Flag`, `SunFlag`, `Favorite`, `Stop` |
| `espsomfy_rts.send_step_command` | `direction` Up/Down, `step_size` 1-127, optional `repeat` 0-50 | sends a step of this size (80-bit motors) |
| `espsomfy_rts.reboot` | | reboots the device |
| `espsomfy_rts.backup` | | saves a backup of the device to `ESPSomfyRTS_<serverId>/` in your Home Assistant config directory |

```yaml
action: espsomfy_rts.send_command
target:
  entity_id: cover.living_room_shade
data:
  command: Prog
  repeat: 40
```

# Events
The integration fires an `espsomfy-rts_event` on the Home Assistant event bus for every command a shade receives, whether it came from Home Assistant, a remote, a group or the ESPSomfy RTS web page. Use it to trigger automations from your remotes.

The event's data:
* `entity_id`: the entity id of the cover
* `event_key`: what triggered the event, always `shadeCommand` for now
* `name`: the cover's name
* `source`: where the command came from
  * `remote`: a button was pressed on a remote
  * `internal`: ESPSomfy RTS sent it (Home Assistant, the web page, MQTT or a script)
  * `group`: it was sent to a group the shade is in
* `remote_address`: the address ESPSomfy RTS uses for the shade
* `source_address`: the address of the remote channel that sent it, or of the group
* `command`: the command, one of `Up`, `Down`, `My`, `Step Up`, `Step Down`, `Prog`, `My+Up`, `My+Down`, `Up+Down`, `My+Up+Down`, `Sun Flag`, `Flag`, `Sensor`, `Toggle`, `Favorite`, `Stop`
* `timestamp`: when it was received

![image](https://github.com/rstrouse/ESPSomfy-RTS-HA/assets/47839015/2fbf4ad8-86b4-4d4e-ac8e-ce04ba4adeeb)

# Development
`custom_components/espsomfy_rts/proto.py` speaks the firmware's socket protocol 2 ([PROTOCOL.md](https://github.com/cjkas/ESPSomfy-RTS/blob/main/docs/PROTOCOL.md)). It imports nothing from Home Assistant, and `tests/proto` tests it against a fake device that replays what a real one sent (`tests/fixtures/proto2`):

```sh
uv run --no-project --python 3.13 --with-requirements requirements_test.txt -- pytest tests/proto
```

The integration picks protocol 2 when the device answers on `ws://host/ws`, and keeps the HTTP API on port 8081 and the old socket on 8080 for firmware before it, whose `/ws` answers 404. On protocol 2, `compat.py` hands the entities the model and events in the shape the old API gave them, so they read the same fields either way.

`tests/ha` tests the integration in Home Assistant's test harness ([pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)), against the same fake device. Home Assistant runs on Linux and macOS only:

```sh
uv run --no-project --python 3.13 --with-requirements requirements_test_ha.txt -- pytest tests/ha
```
