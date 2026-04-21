<div align="center">

# 🍓 Raspberry Pi Hardware Test Suite

[![Python](https://img.shields.io/badge/Python-3.7+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-C51A4A?style=for-the-badge&logo=raspberrypi&logoColor=white)](https://raspberrypi.com)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-22C55E?style=for-the-badge)]()
[![GPIO](https://img.shields.io/badge/GPIO-Multi--Backend-F97316?style=for-the-badge)]()


[![Raspberry Pi Guide](https://img.shields.io/badge/Raspberry_Pi_Guide-C51A4A?style=flat&logo=raspberrypi&logoColor=white)](https://krishnavamsi333.github.io/RPI/)

**A comprehensive, interactive terminal tool for testing every major Raspberry Pi hardware interface.**  
GPIO · PWM · I2C · SPI · UART · Servo · Interrupts · CI/Headless Mode

</div>

---

## 🖥️ Terminal Preview

```
╔════════════════════════════════╗
║  Raspberry Pi Hardware Tests   ║
╚════════════════════════════════╝
  Backend: RPi.GPIO

  ──── Hardware Tests ──────────────
           1.  GPIO Loopback Test
          1b.  GPIO Interrupt / Edge Test
           2.  Manual GPIO Control
           3.  PWM Test  (LED ramp)
          3b.  PWM Test  (Servo sweep)
           4.  I2C Scan  (i2cdetect)
          4b.  I2C Alternatives
           5.  SPI Loopback Test
           6.  UART Loopback Test
           7.  Run ALL Tests
  ──── Diagnostics ─────────────────
  [inf]    s.  System Info
  [inf]    p.  GPIO Pin Snapshot
  [inf]    c.  Check Dependencies
  [inf]    b.  GPIO Backend Reference
  [inf]    l.  View Session Log
  [inf]  cfg.  Edit / Save Config
  ──── GPIO Reference ──────────────
  [ref]    8.  40-Pin Reference Table
  [ref]    9.  Search Pins
  [ref]    f.  Filter Pins by Type
           0.  Exit
```

---

## ✨ Feature Overview

<table>
<tr>
<td width="50%">

### 🔌 Hardware Tests
| | Test |
|---|---|
| 🟢 | GPIO Loopback (3 pairs) |
| 🟢 | GPIO Interrupt / Edge Detect |
| 🟡 | PWM LED Brightness Ramp |
| 🟡 | PWM Servo Sweep 0°→180°→0° |
| 🔵 | I2C Bus Scan |
| 🟠 | SPI Loopback |
| 🔴 | UART Loopback |

</td>
<td width="50%">

### 🛠️ Diagnostics & Tools
| | Tool |
|---|---|
| 📊 | System Info (model, temp, throttle) |
| 📍 | Live GPIO Pin Snapshot |
| 📦 | Dependency Checker |
| 🔧 | GPIO Backend Reference |
| 📋 | Session Log Viewer |
| ⚙️ | Config File Editor |
| 🗺️ | 40-Pin Reference Table |

</td>
</tr>
</table>

---

## 🚀 Quick Start

```bash
# Clone the repo
git clone https://github.com/krishnavamsi333/rpi-hardware-test.git
cd rpi-hardware-test

# Install a GPIO backend (see Backend section below)
pip install RPi.GPIO

# Run!
python3 rpi_hardware_test.py
```

> 💡 No dependencies are strictly required to launch — missing libraries are detected gracefully with install hints printed in the menu.

---

## 🔧 GPIO Backend Support

The script **auto-selects the best available backend** at startup. Override anytime with an environment variable.

| Badge | Backend | Env Value | Install Command | Notes |
|:---:|---|---|---|---|
| ![RPi.GPIO](https://img.shields.io/badge/RPi.GPIO-default-22C55E?style=flat-square) | **RPi.GPIO** | `rpigpio` | `pip install RPi.GPIO` | Default for Pi 1–4 |
| ![pigpio](https://img.shields.io/badge/pigpio-advanced-3B82F6?style=flat-square) | **pigpio** | `pigpio` | `pip install pigpio` + `sudo systemctl start pigpiod` | Hardware-timed PWM, servo, remote GPIO. Best accuracy |
| ![lgpio](https://img.shields.io/badge/lgpio-pi5-A855F7?style=flat-square) | **lgpio** | `lgpio` | `pip install lgpio` | ✅ Pi 5 compatible, modern kernel interface |
| ![gpiod](https://img.shields.io/badge/gpiod-noroot-F97316?style=flat-square) | **gpiod** | `gpiod` | `pip install gpiod` | No root needed, software PWM via threading |
| ![gpiozero](https://img.shields.io/badge/gpiozero-highlevel-EC4899?style=flat-square) | **gpiozero** | `gpiozero` | `pip install gpiozero` | High-level abstraction fallback |
| ![busio](https://img.shields.io/badge/busio-blinka-14B8A6?style=flat-square) | **busio** | — | `pip install adafruit-blinka` | CircuitPython I2C/SPI/UART (Blinka) |
| ![smbus2](https://img.shields.io/badge/smbus2-i2c-EAB308?style=flat-square) | **smbus2** | — | `pip install smbus2` | Pure-Python I2C, no GPIO dependency |

**Override example:**
```bash
RPI_GPIO_BACKEND=lgpio python3 rpi_hardware_test.py
```

---

## 🖥️ CLI Flags

```bash
python3 rpi_hardware_test.py                # interactive menu (default)
python3 rpi_hardware_test.py --headless     # run all tests, exit 0=pass / 1=fail
python3 rpi_hardware_test.py --check        # dependency check only
python3 rpi_hardware_test.py --info         # system info only
python3 rpi_hardware_test.py --snapshot     # live GPIO pin state snapshot
python3 rpi_hardware_test.py --backends     # list all GPIO backends
python3 rpi_hardware_test.py --save-config  # write default rpi_test.json
```

> 🤖 Use `--headless` in cron jobs or CI pipelines. Exit code `0` = all tests passed, `1` = one or more failed.

---

## ⚙️ Configuration File

Generate the default config:
```bash
python3 rpi_hardware_test.py --save-config
```

This creates `rpi_test.json` in the working directory:

```json
{
  "gpio_pairs":   [[17, 27], [22, 23], [24, 25]],
  "pwm_pin":      18,
  "pwm_freq_hz":  1000,
  "servo_pin":    18,
  "uart_device":  "/dev/serial0",
  "uart_baud":    9600,
  "spi_bus":      0,
  "spi_device":   0,
  "spi_speed_hz": 500000,
  "i2c_bus":      1
}
```

Edit any value — the script picks it up on next launch. No restart needed.

---

## 🔌 Wiring Guide

### 🟢 GPIO Loopback

Connect these pairs with **jumper wires** before running test `1`:

```
GPIO17  (Pin 11)  ←→  GPIO27  (Pin 13)
GPIO22  (Pin 15)  ←→  GPIO23  (Pin 16)
GPIO24  (Pin 18)  ←→  GPIO25  (Pin 22)
```

---

### 🟡 PWM — LED Brightness Ramp

```
GPIO18 (Pin 12) ──► 330Ω resistor ──► LED (+)
                                       LED (-) ──► GND (Pin 6)
```

---

### 🟡 PWM — Servo Sweep

```
Servo signal  (orange/yellow) ──► GPIO18  (Pin 12)
Servo VCC     (red)           ──► 5V      (Pin 2 or 4)
Servo GND     (brown/black)   ──► GND     (Pin 6)
```

> ⚠️ **Don't power a servo from the 3.3V rail** — it needs 5V and can brown-out the Pi if underpowered.

---

### 🔵 I2C

```
SDA ──► Pin 3   (1.8kΩ pull-up built in)
SCL ──► Pin 5   (1.8kΩ pull-up built in)
```

Enable first:
```bash
sudo raspi-config → Interface Options → I2C → Enable
```

---

### 🟠 SPI Loopback

```
MOSI  (Pin 19)  ←→  MISO  (Pin 21)    # bridge with a single jumper
```

Enable first:
```bash
sudo raspi-config → Interface Options → SPI → Enable
```

---

### 🔴 UART Loopback

```
TX  (Pin 8)  ←→  RX  (Pin 10)    # bridge with a single jumper
```

> ⚠️ **Disable the serial login console first** or the test will see garbage:
```bash
sudo raspi-config → Interface → Serial Port
  → "login shell over serial" : No
  → "serial port hardware"    : Yes
```

---

## 🗺️ 40-Pin Reference Table

Run option `8` in the menu for a full colour-coded dual-column table:

```
  PIN  BCM  LABEL    TYPE   DESCRIPTION                  │  DESCRIPTION                 TYPE   LABEL    BCM  PIN
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
    1    —  3V3      PWR33  3.3V power (~50mA max)        │  5V rail (USB/barrel)        PWR5   5V         —    2
    3    2  SDA1     I2C    I2C1 data  1.8kΩ pull-up      │  5V power rail               PWR5   5V         —    4
    5    3  SCL1     I2C    I2C1 clock 1.8kΩ pull-up      │  Ground                      GND    GND        —    6
```

**Search** — option `9`:
```
BCM number, label or keyword: spi
  Pin 19  MOSI      BCM 10   SPI0 MOSI
  Pin 21  MISO      BCM  9   SPI0 MISO
  Pin 23  SCLK      BCM 11   SPI0 clock
```

**Filter by type** — option `f`:
```
Type: pwm
  Pin 12  PWM0      BCM18   HW PWM ch0 / PCM CLK
  Pin 32  PWM0      BCM12   HW PWM ch0 (alt pin)
  Pin 33  PWM1      BCM13   HW PWM ch1
```

---

## 📋 Logging

All test results are appended to `rpi_test.log`. View inside the menu with option `l`, or tail it directly:

```bash
tail -f rpi_test.log
```

Example output:
```log
2025-01-15 14:23:01 [INFO] GPIO backend selected: RPi.GPIO
2025-01-15 14:23:05 [INFO] GPIO loopback GPIO17→GPIO27  HIGH: PASS
2025-01-15 14:23:05 [INFO] GPIO loopback GPIO17→GPIO27  LOW:  PASS
2025-01-15 14:23:12 [INFO] PWM LED test: PASS
2025-01-15 14:23:18 [INFO] SPI spidev: PASS — Sent ['0xde','0xad','0xbe','0xef'] | Got same
2025-01-15 14:23:25 [INFO] UART pyserial: PASS — Sent: b'UART_LOOPBACK_TEST_OK' | Got: same
2025-01-15 14:23:26 [INFO] Run-all: {'GPIO Loopback': True, 'SPI Loopback': True, ...}
```

---

## 🐛 Troubleshooting

| ❌ Symptom | ✅ Fix |
|---|---|
| `RuntimeError: Please set pin numbering mode` | Upgrade to this version — fixed by keeping `GPIO.setmode(BCM)` persistent between tests |
| `PermissionError` on any GPIO pin | `sudo usermod -aG gpio $USER` then **log out and back in** |
| `i2cdetect: command not found` | `sudo apt install i2c-tools` |
| UART test times out or returns garbage | Disable serial login shell in raspi-config; check TX↔RX jumper |
| SPI loopback always fails | Enable SPI in raspi-config; confirm MOSI↔MISO jumper is connected |
| `pigpiod not running` error | `sudo systemctl start pigpiod` (or `enable` for auto-start on boot) |
| Pi 5 GPIO errors with RPi.GPIO | Use lgpio: `RPI_GPIO_BACKEND=lgpio python3 rpi_hardware_test.py` |
| Servo jitters or doesn't move | Switch to pigpio backend for hardware-accurate 50Hz timing |
| Throttling warning in System Info | Check power supply (use official Pi PSU) and add a heatsink/fan |

---

## 📦 Full Install (all optional features)

```bash
# GPIO backends
pip install RPi.GPIO pigpio lgpio gpiod gpiozero

# Protocol libraries
pip install spidev pyserial smbus2

# CircuitPython / Blinka
pip install adafruit-blinka

# System tools
sudo apt install i2c-tools libraspberrypi-bin

# Start pigpio daemon
sudo systemctl enable pigpiod
sudo systemctl start pigpiod
```

---

## 📁 File Structure

```
rpi-hardware-test/
├── rpi_hardware_test.py   ← main script
├── rpi_test.json          ← config file  (auto-generated)
├── rpi_test.log           ← test log     (auto-generated)
└── README.md
```

---

## 🧩 Requirements

- Raspberry Pi with 40-pin GPIO header (any model)
- Python **3.7+**
- At least one GPIO backend installed

---

## 📄 License

MIT License — use freely, attribution appreciated.

---

<div align="center">

Made with ❤️ for tinkerers &nbsp;·&nbsp; Tested on Pi 3B+, Pi 4, Pi 5 &nbsp;·&nbsp; PRs welcome

[![GitHub stars](https://img.shields.io/github/stars/krishnavamsi333/rpi-hardware-test?style=social)](https://github.com/krishnavamsi333/rpi-hardware-test)

</div>
