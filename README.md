# Arctis Control

A small Windows 11 replacement for SteelSeries GG, built for the **SteelSeries Arctis 7 (2019, USB ID 1038:12AD)**. It sits as a little badge in the taskbar and uses almost no CPU.

## Features

- **Battery badge** in the taskbar of the screen you pick: live percentage, charging indicator, and a disconnected icon when the headset is off. Turns red at 20%.
- **Steam-style popups** for low battery (20 / 10 / 5%), fully charged, headset on/off and charger plugged/unplugged. They never take focus, so games are not interrupted. You choose which events show a popup and on which screen.
- **Mixer** (click the badge): Game, Chat, Media and Microphone volumes, each with its own output or input device (like Sonar), plus OBS-style level meters.
- **DTS Headphone:X** presets (Off / Game / Music / Movie), using the DTS driver that was installed with the headset.
- **Auto-off**: turns the headset off after N minutes with no sound and no mic input, and a **Turn off** button in the mixer.
- **Battery health estimate**: hours per full charge compared with the rated 24 h, sudden voltage drops, and how far the reading falls after unplugging.

## Install

Download `ArctisControl-<version>-win64.zip` from [Releases](../../releases), unzip it anywhere (for example `%LOCALAPPDATA%\Programs\ArctisControl`) and run `ArctisControl.exe`. Add a shortcut with the `--background` argument to your Startup folder to start it with Windows without opening the mixer.

Settings, battery history and mixer state are stored in `%APPDATA%\ArctisControl`.

## Run from source

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw arctis_battery.py
```

Build the exe yourself with `./build.ps1` (output in `dist\ArctisControl`). Pushing a `v*` tag builds it on GitHub Actions and publishes a release.

## Notes

- The headset protocol (battery, status, idle timer) follows [HeadsetControl](https://github.com/Sapd/HeadsetControl). The charging indicator works by detecting the headset on its USB cable.
- Per-app device routing uses the same Windows API as the Volume mixer settings page (also used by [EarTrumpet](https://github.com/File-New-Project/EarTrumpet)). Some apps only switch device after a restart.
- DTS needs the DTS audio driver that SteelSeries GG installs for the Arctis 7. Without it the DTS row is hidden.
- The Arctis 7 has no known "power off now" command, so **Turn off** sets the headset's idle timer to 1 minute and restores it on the next connect.

Not affiliated with SteelSeries. SteelSeries logo from [Simple Icons](https://simpleicons.org); SteelSeries and Arctis are trademarks of SteelSeries ApS.
