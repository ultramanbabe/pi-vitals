# pi-vitals

Always-on hardware monitor for a Linux desktop, displayed on a tiny Pi-driven side screen.

A FastAPI/WebSocket server runs on the PC and reads CPU, GPU, RAM, drives, and network stats via sysfs / RAPL / `smartctl`. A Raspberry Pi Zero 2W with a 7″ 1024×600 HDMI panel runs Chromium in kiosk mode and shows the dashboard at ~60 fps with smooth gauges and sparklines.

## Hardware tested on

- **PC**: CachyOS (Arch-based), AMD Ryzen 7 9800X3D, ASUS PRIME Radeon RX 9070 XT, MSI PRO X870-P WiFi (nct6687-based motherboard)
- **Pi**: Raspberry Pi Zero 2W, Raspberry Pi OS Lite (Trixie 32-bit), 7″ 1024×600 HDMI display

The server side is Linux-specific (sysfs, hwmon, powercap). Frontend is plain HTML/JS — no framework.

## Run the server (PC)

```sh
./setup.sh        # creates .venv and installs deps
./run.sh          # listens on 0.0.0.0:8765
```

Open `http://localhost:8765` in any browser to verify.

## Pi kiosk

On the Pi (Raspberry Pi OS Lite):

```sh
sudo apt install -y cage chromium
```

Then a systemd unit `/etc/systemd/system/hwmon-kiosk.service` launches `cage -s -- /usr/local/bin/hwmon-kiosk.sh`, which `exec`s Chromium with:

```
--kiosk --noerrdialogs --disable-infobars --no-first-run
--no-default-browser-check --check-for-update-interval=31536000
--start-fullscreen --force-device-scale-factor=1 --window-size=1024,600
--incognito --disable-application-cache --disk-cache-size=1
http://<pc-lan-ip>:8765
```

Cursor is hidden via a blank xcursor theme placed at `/usr/share/icons/blank/`.

## Optional one-time host setup

These unlock additional sensors:

- **SATA SSD/HDD temperatures** require `smartctl`. Allow your user to call it without password:
  ```sh
  sudo pacman -S smartmontools
  echo "$USER ALL=(root) NOPASSWD: /usr/bin/smartctl -A -j /dev/sd*" | sudo tee /etc/sudoers.d/hwmon-smartctl
  ```
- **CPU package power (RAPL)** requires the energy counter to be readable:
  ```sh
  echo 'SUBSYSTEM=="powercap", ACTION=="add", KERNEL=="intel-rapl:*", RUN+="/bin/chmod 0444 /sys/class/powercap/%k/energy_uj"' | sudo tee /etc/udev/rules.d/99-rapl-readable.rules
  sudo udevadm control --reload-rules
  ```
- **Motherboard fan RPM** requires the right kernel sensor module. `sudo sensors-detect --auto` will identify and load it.

## Static hardware labels

Things sysfs can't tell us cleanly (specific GPU SKU, RAM model/speed/CL) are passed in via env vars in [run.sh](run.sh):

```sh
export GPU_MODEL="ASUS PRIME Radeon RX 9070 XT"
export RAM_MODEL="Klevv Bolt V · 6000 CL28"
export CPU_FAN_INPUT="fan1_input"   # which nct6687 fan is the CPU
```

## Layout

3×2 grid sized for 1024×600 at ~65 cm viewing distance:

```
[ CPU       ] [ GPU         ] [ RAM       ]
[ VRAM      ] [ Network     ] [ Disk      ]
```

Network card uses 3/4-circle gauges (down + up). Other cards use sparklines. Per-thread CPU bars sit below the % number on the CPU card.

## License

No license declared yet — treat as all rights reserved unless a `LICENSE` file is added.
