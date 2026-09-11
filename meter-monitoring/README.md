# Smart Meter Monitoring — Infrastructure Documentation

Records live consumption data from a Vorarlberg Netz smart meter (Kaifa MA309M or
Honeywell DM515) into a local SQLite database, for later comparison against
day-ahead electricity prices.

## Architecture

```
Smart meter (Kundenschnittstelle, wired M-Bus)
  → RJ12 cable (pins 3/4 only)
  → M-Bus Slave Click (level converter)
  → ESP32 running ESPHome (decrypts + parses)
  → Wi-Fi → MQTT (Mosquitto, on the Pi)
  → meter_logger.py (subscriber)
  → SQLite (meter_readings.db)
```

The Pi's job is everything from "MQTT" onward. The ESP32/meter side is
documented separately once that config is built (see **Status** at the
bottom).

---

## 1. Hardware (per site — one full set needed per house)

| Component | Notes |
|---|---|
| Raspberry Pi 4 (2GB+) | Ethernet-capable; prefer wired over Wi-Fi if the install location allows it |
| Official Pi power supply (5V/3A USB-C) | Underpowered supplies cause random instability — don't substitute |
| USB SSD (not a USB flash drive) | See **JMicron/UASP warning** below before buying — this bit us hard |
| ESP32 dev board | e.g. AZ-Delivery ESP32 DevKit or similar (classic Xtensa, not the C3/RISC-V variant, for the widest compatibility with existing community configs) |
| MikroE "M-Bus Slave Click" | The actual meter interface board |
| RJ12 (6P6C) to screw-terminal breakout adapter | e.g. PENGLIN or similar — lets you tap 2 wires without cutting/soldering |
| Jumper wires (female-female) | For Click board → ESP32 |

### ⚠️ USB SSD chipset warning (JMicron + Raspberry Pi 4 = data corruption)

**Before buying a USB SSD enclosure, avoid JMicron bridge chips** (USB vendor
ID `152D`). These have well-documented broken UASP (USB Attached SCSI)
support on the Pi 4's USB 3.0 controller — under sustained write load (like
this project's continuous logging), they silently drop write commands,
causing progressive, hard-to-diagnose filesystem corruption. Symptoms:
commands randomly start failing with "Input/output error" or "command not
found" on binaries that definitely exist, worsening over time.

If you already have a JMicron-based enclosure (check via Windows Device
Manager → the drive → Properties → Details → "Hardware Ids", look for
`VID_152D`), the fix is a kernel boot parameter that disables UASP for that
specific device and falls back to the older, slower, but reliable Bulk-Only
Transport protocol. Add this to `cmdline.txt` on the boot partition (single
line, space-separated from the rest of the existing content, exact PID
varies by chip — ours was `0580`):
```
usb-storage.quirks=152d:0580:u
```
Trade-off: disables TRIM. Acceptable for this project's write volume.

---

## 2. Unlock the meter's Kundenschnittstelle (do this first — takes days)

1. Log into your Vorarlberg Netz account at vorarlbergnetz.at
2. Go to **Mein Vertrag → SMARTMETER KUNDENSCHNITTSTELLE**
3. Request the cryptographic key — it's mailed to you by post, can take
   several days
4. Nothing else in this document works without this key

---

## 3. Physical wiring

### RJ12 pinout (confirmed from TINETZ's official spec — applies to both
Kaifa and Honeywell meters, since Vorarlberg Netz, TINETZ, Salzburg AG and
IKB share the same "Kooperation Smartmeter West" standard):

| Pin | Function |
|---|---|
| 1 | Not used |
| 2 | Not used |
| **3** | **MBUS1 (+)** |
| **4** | **MBUS2 (−)** |
| 5 | Not used |
| 6 | Not used |

Only pins 3 and 4 carry signal. Wire them from the RJ12 breakout's screw
terminals to the M-Bus Slave Click's M-Bus + / − terminals.

**Known snag**: the meter's socket can be recessed enough that a standard
RJ12 plug's plastic housing doesn't physically fit — you may need to trim
the plastic shell down before it seats properly. Test-fit before wiring.

### M-Bus Slave Click → ESP32

| Click pin | ESP32 pin | Notes |
|---|---|---|
| TX | RX (e.g. GPIO35) | Crossed — Click's TX goes to ESP32's RX |
| RX | TX (e.g. GPIO16) | Crossed — Click's RX goes to ESP32's TX |
| 3.3V / 5V | matching ESP32 pin | Check Click board silkscreen before connecting |
| GND | GND | |

**Avoid GPIO36 for RX** — it's input-only with no internal pull resistor,
and caused intermittent corrupted frames in community testing. Use GPIO35
instead.

### UART framing (from the official spec — not the common default!)

```yaml
uart:
  tx_pin: GPIO16
  rx_pin: GPIO35
  baud_rate: 2400
  parity: EVEN      # <- easy to miss; default is usually NONE
  rx_buffer_size: 2048
```

---

## 4. Raspberry Pi OS setup (Raspberry Pi Imager)

- OS: **Raspberry Pi OS Lite (64-bit)**
- Advanced Options: set hostname (e.g. `meterpi-<site>`), enable SSH,
  set username/password, locale = Europe/Vienna

### Network config

Modern Raspberry Pi OS images use **cloud-init**, not the older
`firstrun.sh`. The file to edit is **`network-config`** on the `bootfs`
partition (check what's actually there before assuming — this changed
recently and depends on your Imager/OS version).

**Critical**: if using Wi-Fi, the `regulatory-domain` must be set correctly
(`"AT"` for Austria) or DHCP can silently and consistently fail — this cost
us hours before being traced back to a missing/wrong country code.

Known-good Wi-Fi-only config:
```yaml
network:
  version: 2
  wifis:
    wlan0:
      dhcp4: true
      regulatory-domain: "AT"
      access-points:
        "<your-ssid>":
          password: "<PSK-hash-imager-generates-for-you>"
      optional: true
```

If you also configure Ethernet alongside Wi-Fi, be aware **both interfaces
claiming a default route can cause routing conflicts and silent SSH
timeouts** — prefer a single active interface unless you have a specific
reason not to.

### Editing gotchas (cost real time, worth avoiding)

- **Never use "Save As" in Notepad/VS Code on these files.** It can silently
  save as `network-config.txt` (extension hidden by Windows Explorer by
  default) while cloud-init keeps reading the original, untouched file.
  Always edit the already-open file and save in place (Ctrl+S).
- **Always use "Safely Eject"** before physically removing the SD
  card/SSD from your PC. An improper removal is a likely cause of
  filesystem corruption independent of the JMicron issue above.
- After any re-flash, SSH will warn that the host key changed (expected —
  fresh OS = fresh key). Clear the stale entry:
  ```powershell
  ssh-keygen -R <hostname-or-ip>
  ```
- Prefer connecting via `<hostname>.local` (mDNS) over tracking IP
  addresses — sidesteps DHCP lease changes entirely.

---

## 5. Software stack

### Mosquitto (MQTT broker)

```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients

sudo mosquitto_passwd -c /etc/mosquitto/passwd meterlogger
# (set a password when prompted)

sudo tee /etc/mosquitto/conf.d/local.conf <<EOF
listener 1883
password_file /etc/mosquitto/passwd
allow_anonymous false
EOF

# IMPORTANT: the password file must be owned by the mosquitto user,
# or the service fails to start with exit code 13 (permission denied).
sudo chown mosquitto:mosquitto /etc/mosquitto/passwd
sudo chmod 640 /etc/mosquitto/passwd

sudo systemctl restart mosquitto
sudo systemctl enable mosquitto
sudo systemctl status mosquitto   # confirm "active (running)"
```

### Python + paho-mqtt

```bash
sudo apt install -y python3-pip   # not preinstalled on Lite images
pip install paho-mqtt --break-system-packages
```

### `meter_logger.py`

Subscribes to `+/sensor/+/state` (ESPHome's default MQTT topic pattern),
maps each device name to a site label, and writes every reading into a
`meter_readings` table in SQLite.

**Before running, edit these three things at the top of the file:**
- `MQTT_PASSWORD` — the password set above for the `meterlogger` MQTT user
- `DB_PATH` — use your **actual home directory**, not a placeholder
  (e.g. `/home/ptschofen/meter_readings.db`, not `/home/pi/...`)
- `DEVICE_TO_SITE` — maps each ESP32's ESPHome node name to a short site
  label, e.g.:
  ```python
  DEVICE_TO_SITE = {
      "smartmeter-home": "home",
      "smartmeter-parents": "parents",
  }
  ```

### `meter-logger.service` (systemd)

**Before installing, edit the placeholder username/paths to match your
actual account** (the template uses `pi` as a placeholder — change every
occurrence to your real username):
```ini
ExecStart=/usr/bin/python3 /home/<youruser>/meter_logger.py
WorkingDirectory=/home/<youruser>
User=<youruser>
```

```bash
sudo cp meter-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meter-logger
sudo systemctl start meter-logger
sudo systemctl status meter-logger   # confirm "active (running)"
```

---

## 6. Testing (no real meter hardware needed)

Simulate a reading and confirm it lands in the database:
```bash
mosquitto_pub -h localhost -u meterlogger -P <your-password> \
  -t "smartmeter-parents/sensor/active_power_plus/state" -m "1523.7"

sqlite3 /home/<youruser>/meter_readings.db "SELECT * FROM meter_readings;"
```
A row shows up → the entire receiving pipeline (MQTT → logger → database)
is confirmed working, independent of whether the meter/ESP32 side exists
yet.

---

## 7. Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| WiFi "connects" in UniFi but never gets a DHCP lease | Missing/wrong `regulatory-domain` in `network-config` |
| SSH works once, then times out later | VPN reconnected (full-tunnel mode blocks LAN traffic) — disconnect and retry |
| SSH works via one interface's IP but not another | Two interfaces (eth0 + wlan0) both claiming default routes — disable the one you're not using |
| `mosquitto.service` fails, exit code 13 | Password file permissions — `chown mosquitto:mosquitto`, `chmod 640` |
| Commands randomly "not found" / "Input/output error" | JMicron USB SSD enclosure + UASP corruption — see hardware section |
| `pip: command not found` | `sudo apt install python3-pip` first |
| systemd service won't start after copying from another machine | Check for leftover `pi`/`/home/pi` placeholders — fix to your actual username |
| SSH warns "host key has changed" | Expected after any re-flash — `ssh-keygen -R <host>` |
| Editing `network-config` doesn't seem to take effect | Check the file wasn't accidentally saved as `<name>.txt` (Windows hides known extensions) |

---

## Status

- [x] Pi hardware set up and diagnosed (JMicron/UASP issue found and fixed)
- [x] Wi-Fi networking stable
- [x] Mosquitto running, authenticated
- [x] `meter_logger.py` running as a systemd service, tested end-to-end
      with a simulated MQTT message
- [ ] Physical RJ12/M-Bus wiring to the actual meter
- [ ] ESPHome config (UART + `dlms_meter` component + decryption key)
- [ ] First real meter reading logged
- [ ] Repeat this entire process for the second site (own house, Kaifa
      meter) — same steps apply per the TINETZ spec covering both meter
      models
