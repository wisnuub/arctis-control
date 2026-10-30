# Arctis Control

A small Windows 11 replacement for SteelSeries GG, built for the **SteelSeries Arctis 7 (2019, USB ID 1038:12AD)**. It sits as a little badge in the taskbar and uses almost no CPU.

## Features

- **Battery badge** in the taskbar of the screen you pick: live percentage, charging indicator, and a disconnected icon when the headset is off. Turns red at 20%.
- **Steam-style popups** for low battery (20 / 10 / 5%), fully charged, headset on/off and charger plugged/unplugged. They never take focus, so games are not interrupted. You choose which events show a popup and on which screen.
- **Mixer** (click the badge): Game, Chat, Media and Microphone volumes, each with its own output or input device (like Sonar), plus OBS-style level meters.
- **DTS Headphone:X** presets (Off / Game / Music / Movie), using the DTS driver that was installed with the headset.
- **Auto-off**: turns the headset off after N minutes with no sound and no mic input, and a **Turn off** button in the mixer.
- **Noise cancelling** for your mic with [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet), similar to Krisp, for every app (needs the free VB-CABLE driver, see below).
- **Battery health estimate**: hours per full charge compared with the rated 24 h, sudden voltage drops, and how far the reading falls after unplugging.

## Install

Download `ArctisControl-<version>-win64.zip` from [Releases](../../releases), unzip it anywhere (for example `%LOCALAPPDATA%\Programs\ArctisControl`) and run `ArctisControl.exe`. Add a shortcut with the `--background` argument to your Startup folder to start it with Windows without opening the mixer.

Settings, battery history and mixer state are stored in `%APPDATA%\ArctisControl`.

## Noise cancelling setup

Noise cancelling sends your mic through DeepFilterNet into a virtual microphone that Discord, games and other apps can use. That virtual microphone comes from **VB-CABLE**, a free (donationware) driver by VB-Audio. Its licence does not allow bundling it inside other software, and this project is not paying for a redistribution licence, so you install it yourself once:

1. Download the VB-CABLE driver pack from [vb-cable.com](https://vb-audio.com/Cable/) and run `VBCABLE_Setup_x64.exe` as administrator, then reboot. If you find it useful, consider donating to VB-Audio.
2. Open the mixer in Arctis Control and set **Noise Cancel** to **On**.

While it is on, the virtual mic ("CABLE Output", which you can rename in Windows Sound settings) becomes your default recording device, so apps set to **Default** follow it automatically. Turning it off, or quitting the app, switches back to the Arctis mic. If an app has its own mic setting, pick the CABLE Output device there. The setting is remembered, and it adds roughly 30 to 50 ms of mic delay.

Voicemeeter's virtual inputs also work if you already use Voicemeeter, but VB-CABLE is simpler because nothing else has to run.

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
- DeepFilterNet (MIT / Apache-2.0) is downloaded from its official release when building; VB-CABLE is not included.
- The Arctis 7 has no known "power off now" command, so **Turn off** sets the headset's idle timer to 1 minute and restores it on the next connect.

Not affiliated with SteelSeries. SteelSeries logo from [Simple Icons](https://simpleicons.org); SteelSeries and Arctis are trademarks of SteelSeries ApS.
