<image src="https://user-images.githubusercontent.com/47839015/218900217-81f88955-67b8-4ed8-8e97-271de66c555e.png" align="right" style="margin-top:-2em;width:177px;margin-right:2em;display:inline-block;float:right;"></image>

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration) [![Tests](https://github.com/cjkas/ESPSomfy-RTS-HA/actions/workflows/tests.yaml/badge.svg?branch=main)](https://github.com/cjkas/ESPSomfy-RTS-HA/actions/workflows/tests.yaml) [![CodeQL](https://github.com/cjkas/ESPSomfy-RTS-HA/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/cjkas/ESPSomfy-RTS-HA/actions/workflows/codeql.yml) [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/cjkas/ESPSomfy-RTS-HA/badge)](https://scorecard.dev/viewer/?uri=github.com/cjkas/ESPSomfy-RTS-HA)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cjkas&repository=ESPSomfy-RTS-HA&category=integration)


# ESPSomfy RTS-HA 
Control up to 32 Somfy shades from Home Assistant, and set their positions, with an ESP32 and an inexpensive CC1101 transceiver. You can also control up to 16 groups, to move several shades at once.

# Requirements
You need an [ESPSomfy RTS](https://github.com/cjkas/ESPSomfy-RTS) device, an ESP32 with a CC1101 transceiver, set up for your shades. The [ESPSomfy RTS wiki](https://github.com/cjkas/ESPSomfy-RTS/wiki) describes how to build and configure one.

| ESPSomfy RTS firmware | Integration version |
|---|---|
| 4.0.0 and later | 3.0.1 and later |
| before 4.0.0 | any; 3.0.1 and later work too |

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

Each device gets a cover for every shade and group, switches for dry contacts and sun sensors, sunny and windy sensors, reboot and backup buttons, and diagnostic sensors. The [Entities](https://github.com/cjkas/ESPSomfy-RTS-HA/wiki/Entities) page lists them all.

* [Services](https://github.com/cjkas/ESPSomfy-RTS-HA/wiki/Services): the actions for automations and scripts, such as moving to a position, correcting the position from another sensor, sun and wind, sending any RTS command, and backups.
* [Events](https://github.com/cjkas/ESPSomfy-RTS-HA/wiki/Events): an `espsomfy-rts_event` is fired for every command a shade receives, including button presses on a Somfy remote, so your remotes can trigger automations.

## Firmware updates
Since firmware 4.0.0 the firmware entity only shows the installed version. To update, download the release from [GitHub](https://github.com/cjkas/ESPSomfy-RTS/releases) and upload it on the device's web page, as described in [Updating ESPSomfy RTS](https://github.com/cjkas/ESPSomfy-RTS/wiki/Updating-ESPSomfy-RTS). Coming from 3.x, see [Upgrading to firmware 4.0.0](https://github.com/cjkas/ESPSomfy-RTS-HA/wiki/Upgrading-to-Firmware-4.0.0) first.

HACS tells you when there is a new version of this integration.

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
