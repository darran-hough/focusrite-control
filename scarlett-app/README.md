# 🎛 Scarlett Control — Linux

A proper native desktop app for Focusrite Scarlett interfaces on Linux.
Opens as a real application window — no browser, no localhost, no terminal needed after install.

## Install (One Time)

1. Unzip this folder
2. Open a terminal inside it and run:
   ```
   chmod +x install.sh
   ./install.sh
   ```
3. Done — search **"Scarlett Control"** in your app launcher

## What it does

| Tab | What you can control |
|-----|---------------------|
| 🎤 Microphones | Input volume (slider), 48V phantom power, Instrument/Guitar mode, Air mode |
| 🎧 Headphones & Speakers | Volume of every source, mute buttons, direct monitoring on/off |
| 🔀 Routing | Connect/disconnect sources to outputs visually |
| ⚙️ Settings | Sample rate, buffer size with plain-English guidance |

## Requirements

- Linux (Ubuntu 22.04+, Fedora 37+, or similar)
- Python 3.10+
- Kernel 5.14+ (for Scarlett ALSA driver)
- PipeWire (default on most modern distros)

## Supported Devices

All Focusrite Scarlett and Clarett USB devices with Linux kernel driver support:
Scarlett Solo, 2i2, 4i4, 8i6, 18i8, 18i20 (2nd, 3rd, 4th gen) and Clarett USB.

## Uninstall

```
./uninstall.sh
```
