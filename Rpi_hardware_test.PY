#!/usr/bin/env python3
"""
Raspberry Pi Hardware Test Suite — Enhanced
Covers: GPIO, PWM, I2C, SPI, UART, Servo, Interrupts,
        System Info, Pin Snapshot, Dep Check, Headless CI
Alternatives: pigpio, gpiod, lgpio, smbus2, busio (CircuitPython)
"""

import os
import sys
import time
import json
import logging
import subprocess
import threading

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("rpi_test.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Color helpers ──────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

PIN_COLORS = {
    "PWR33": "\033[91m",
    "PWR5":  "\033[31m",
    "GND":   "\033[90m",
    "GPIO":  "\033[94m",
    "I2C":   "\033[92m",
    "SPI":   "\033[32m",
    "UART":  "\033[33m",
    "PWM":   "\033[95m",
    "ID":    "\033[37m",
}

def ok(msg):   print(f"  {GREEN}✔ PASS{RESET}  {msg}")
def fail(msg): print(f"  {RED}✖ FAIL{RESET}  {msg}")
def info(msg): print(f"  {CYAN}ℹ{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")

# ── Config file support ────────────────────────────────────────────────────────
CONFIG_FILE = "rpi_test.json"
DEFAULT_CONFIG = {
    "gpio_pairs":   [[17, 27], [22, 23], [24, 25]],
    "pwm_pin":      18,
    "pwm_freq_hz":  1000,
    "servo_pin":    18,
    "uart_device":  "/dev/serial0",
    "uart_baud":    9600,
    "spi_bus":      0,
    "spi_device":   0,
    "spi_speed_hz": 500000,
    "i2c_bus":      1,
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                user = json.load(f)
            cfg = {**DEFAULT_CONFIG, **user}
            info(f"Loaded config from {CONFIG_FILE}")
            return cfg
        except Exception as e:
            warn(f"Config load error: {e} — using defaults")
    return DEFAULT_CONFIG.copy()

def save_default_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    ok(f"Default config written to {CONFIG_FILE}")

CFG = load_config()

# ── Headless mode ──────────────────────────────────────────────────────────────
HEADLESS = "--headless" in sys.argv

def _input(prompt: str) -> str:
    if HEADLESS:
        print(f"  {DIM}[headless] {prompt}(auto-confirmed){RESET}")
        return ""
    return input(prompt)


# ══════════════════════════════════════════════════════════════════════════════
# GPIO BACKEND LAYER
# ══════════════════════════════════════════════════════════════════════════════
#
#  Priority: RPi.GPIO → pigpio → lgpio → gpiod → gpiozero
#
#  KEY FIX: cleanup() only releases pins — it does NOT tear down the adapter.
#  GPIO.setmode(BCM) is called once at init and never cleared between tests.
#  Each adapter provides _release_pins() for per-test cleanup instead.

class _GPIOAdapter:
    name      = "none"
    available = False
    def setup(self, pin, mode):      pass
    def output(self, pin, val):      pass
    def input(self, pin) -> int:     return 0
    def pwm(self, pin, freq_hz=1000): return None
    def _release_pins(self, *pins):  pass   # lightweight per-test cleanup
    def cleanup(self):               pass   # called only on program exit


# ── Backend: RPi.GPIO ──────────────────────────────────────────────────────────
class _RPiGPIOAdapter(_GPIOAdapter):
    name = "RPi.GPIO"

    def __init__(self):
        import RPi.GPIO as _G
        self._G = _G
        self._G.setmode(self._G.BCM)   # set ONCE — never cleared mid-session
        self._G.setwarnings(False)
        self.available = True

    def _ensure_mode(self):
        """Re-apply BCM mode if a previous cleanup() wiped it."""
        try:
            self._G.setmode(self._G.BCM)
        except Exception:
            pass  # already set — RPi.GPIO raises if you set same mode twice

    def setup(self, pin, mode):
        self._ensure_mode()
        m = self._G.OUT if mode == "out" else self._G.IN
        self._G.setup(pin, m)

    def output(self, pin, val):
        self._ensure_mode()
        self._G.output(pin, val)

    def input(self, pin):
        self._ensure_mode()
        return self._G.input(pin)

    def pwm(self, pin, freq_hz=1000):
        self._ensure_mode()
        self._G.setup(pin, self._G.OUT)
        return self._G.PWM(pin, freq_hz)

    def add_event_detect(self, pin, edge, callback):
        self._ensure_mode()
        e = self._G.RISING if edge == "rising" else self._G.FALLING
        self._G.add_event_detect(pin, e, callback=callback)

    def _release_pins(self, *pins):
        """Clean up only the specified pins without wiping GPIO mode."""
        for p in pins:
            try:
                self._G.cleanup(p)
            except Exception:
                pass
        # Re-apply mode immediately so next test finds it intact
        self._ensure_mode()

    def cleanup(self):
        """Full cleanup — only called at program exit."""
        try:
            self._G.cleanup()
        except Exception:
            pass


# ── Backend: pigpio ────────────────────────────────────────────────────────────
class _PigpioAdapter(_GPIOAdapter):
    """
    pigpio: hardware-timed PWM, native servo pulses, remote GPIO.
    Requires: pip install pigpio  +  sudo systemctl start pigpiod
    """
    name = "pigpio"

    def __init__(self):
        import pigpio as _pg
        self._pg = _pg
        self._pi = _pg.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpiod not running — sudo systemctl start pigpiod")
        self.available = True

    def setup(self, pin, mode):
        m = self._pg.OUTPUT if mode == "out" else self._pg.INPUT
        self._pi.set_mode(pin, m)

    def output(self, pin, val): self._pi.write(pin, val)
    def input(self, pin):       return self._pi.read(pin)

    def pwm(self, pin, freq_hz=1000):
        pi = self._pi
        class _PigpioPWM:
            def start(self, dc):
                pi.set_PWM_frequency(pin, freq_hz)
                pi.set_PWM_dutycycle(pin, int(dc * 255 / 100))
            def ChangeDutyCycle(self, dc):
                pi.set_PWM_dutycycle(pin, int(dc * 255 / 100))
            def stop(self):
                pi.set_PWM_dutycycle(pin, 0)
        return _PigpioPWM()

    def servo_pulse(self, pin, pulse_us):
        self._pi.set_servo_pulsewidth(pin, pulse_us)

    def _release_pins(self, *pins): pass   # pigpio has no per-pin cleanup
    def cleanup(self): self._pi.stop()


# ── Backend: lgpio ─────────────────────────────────────────────────────────────
class _LGpioAdapter(_GPIOAdapter):
    """
    lgpio: modern replacement for RPi.GPIO, works on Pi 5.
    Requires: pip install lgpio
    """
    name = "lgpio"

    def __init__(self):
        import lgpio as _lg
        self._lg = _lg
        self._h  = _lg.gpiochip_open(0)
        self.available = True

    def setup(self, pin, mode):
        if mode == "out":
            self._lg.gpio_claim_output(self._h, pin)
        else:
            self._lg.gpio_claim_input(self._h, pin)

    def output(self, pin, val): self._lg.gpio_write(self._h, pin, val)
    def input(self, pin):       return self._lg.gpio_read(self._h, pin)

    def pwm(self, pin, freq_hz=1000):
        lg = self._lg; h = self._h
        class _LGpioPWM:
            def start(self, dc):          lg.tx_pwm(h, pin, freq_hz, dc)
            def ChangeDutyCycle(self, dc): lg.tx_pwm(h, pin, freq_hz, dc)
            def stop(self):               lg.tx_pwm(h, pin, freq_hz, 0)
        return _LGpioPWM()

    def _release_pins(self, *pins):
        for p in pins:
            try: self._lg.gpio_free(self._h, p)
            except Exception: pass

    def cleanup(self): self._lg.gpiochip_close(self._h)


# ── Backend: gpiod ─────────────────────────────────────────────────────────────
class _GpiodAdapter(_GPIOAdapter):
    """
    python-gpiod: kernel character device, no root needed.
    Requires: pip install gpiod  (libgpiod >= 2.x)
    Note: no hardware PWM — software PWM via threading.
    """
    name = "gpiod"

    def __init__(self):
        import gpiod as _gd
        self._gd    = _gd
        self._chip  = _gd.Chip("/dev/gpiochip0")
        self._lines = {}
        self.available = True

    def _line(self, pin, mode):
        key = (pin, mode)
        if key not in self._lines:
            line = self._chip.get_line(pin)
            if mode == "out":
                line.request(consumer="rpi_test",
                             type=self._gd.LINE_REQ_DIR_OUT)
            else:
                line.request(consumer="rpi_test",
                             type=self._gd.LINE_REQ_DIR_IN)
            self._lines[key] = line
        return self._lines[key]

    def setup(self, pin, mode): self._line(pin, mode)
    def output(self, pin, val): self._line(pin, "out").set_value(val)
    def input(self, pin):       return self._line(pin, "in").get_value()

    def pwm(self, pin, freq_hz=1000):
        self.setup(pin, "out")
        adapter = self
        class _SoftPWM:
            def __init__(self):
                self._dc = 0; self._running = False; self._t = None
            def _loop(self):
                period = 1.0 / freq_hz
                while self._running:
                    if self._dc > 0:
                        adapter.output(pin, 1)
                        time.sleep(period * self._dc / 100)
                    if self._dc < 100:
                        adapter.output(pin, 0)
                        time.sleep(period * (100 - self._dc) / 100)
            def start(self, dc):
                self._dc = dc; self._running = True
                self._t = threading.Thread(target=self._loop, daemon=True)
                self._t.start()
            def ChangeDutyCycle(self, dc): self._dc = dc
            def stop(self):
                self._running = False; adapter.output(pin, 0)
        return _SoftPWM()

    def _release_pins(self, *pins):
        for p in pins:
            for mode in ("in", "out"):
                key = (p, mode)
                if key in self._lines:
                    try: self._lines.pop(key).release()
                    except Exception: pass

    def cleanup(self):
        for line in self._lines.values():
            try: line.release()
            except Exception: pass
        self._chip.close()


# ── Backend: gpiozero ──────────────────────────────────────────────────────────
class _GPIOZeroAdapter(_GPIOAdapter):
    """
    gpiozero: high-level library, good for reading.
    Requires: pip install gpiozero
    """
    name = "gpiozero"

    def __init__(self):
        from gpiozero import Button, LED
        self._Button  = Button
        self._LED     = LED
        self._devices = {}
        self.available = True

    def setup(self, pin, mode): self._devices[(pin, mode)] = mode

    def output(self, pin, val):
        key = (pin, "out")
        if key not in self._devices or not hasattr(self._devices[key], "on"):
            self._devices[key] = self._LED(pin)
        self._devices[key].on() if val else self._devices[key].off()

    def input(self, pin):
        key = (pin, "in")
        if key not in self._devices or not hasattr(self._devices[key], "is_pressed"):
            self._devices[key] = self._Button(pin, pull_up=False)
        return int(self._devices[key].is_pressed)

    def pwm(self, pin, freq_hz=1000):
        from gpiozero import PWMLED
        dev = PWMLED(pin)
        self._devices[(pin, "pwm")] = dev
        class _GZeroPWM:
            def start(self, dc):           dev.value = dc / 100
            def ChangeDutyCycle(self, dc): dev.value = dc / 100
            def stop(self):                dev.value = 0
        return _GZeroPWM()

    def _release_pins(self, *pins):
        for p in pins:
            for mode in ("in", "out", "pwm"):
                key = (p, mode)
                if key in self._devices:
                    try: self._devices.pop(key).close()
                    except Exception: pass

    def cleanup(self):
        for dev in self._devices.values():
            try: dev.close()
            except Exception: pass


# ── Auto-select best available backend ─────────────────────────────────────────
def _pick_backend(prefer: str = None) -> _GPIOAdapter:
    order = [
        ("rpigpio",  _RPiGPIOAdapter),
        ("pigpio",   _PigpioAdapter),
        ("lgpio",    _LGpioAdapter),
        ("gpiod",    _GpiodAdapter),
        ("gpiozero", _GPIOZeroAdapter),
    ]
    if prefer:
        order = [(k, v) for k, v in order if k == prefer] + \
                [(k, v) for k, v in order if k != prefer]
    for name, cls in order:
        try:
            adapter = cls()
            info(f"GPIO backend: {adapter.name}")
            log.info(f"GPIO backend selected: {adapter.name}")
            return adapter
        except Exception:
            pass
    warn("No GPIO backend available — hardware tests will be skipped")
    return _GPIOAdapter()

GPIO_ADAPTER = _pick_backend(os.environ.get("RPI_GPIO_BACKEND"))
HAS_GPIO     = GPIO_ADAPTER.available


# ══════════════════════════════════════════════════════════════════════════════
# 40-PIN REFERENCE TABLE
# ══════════════════════════════════════════════════════════════════════════════
PIN_MAP = [
    ( 1, None, "3V3",    "PWR33", "3.3V power (~50mA max)"),
    ( 2, None, "5V",     "PWR5",  "5V rail (USB/barrel)"),
    ( 3,  2,   "SDA1",   "I2C",   "I2C1 data  1.8kΩ pull-up"),
    ( 4, None, "5V",     "PWR5",  "5V power rail"),
    ( 5,  3,   "SCL1",   "I2C",   "I2C1 clock 1.8kΩ pull-up"),
    ( 6, None, "GND",    "GND",   "Ground"),
    ( 7,  4,   "GPIO4",  "GPIO",  "GPIO / 1-Wire (DS18B20)"),
    ( 8, 14,   "TXD0",   "UART",  "UART TX (disable console first)"),
    ( 9, None, "GND",    "GND",   "Ground"),
    (10, 15,   "RXD0",   "UART",  "UART RX"),
    (11, 17,   "GPIO17", "GPIO",  "General-purpose I/O"),
    (12, 18,   "PWM0",   "PWM",   "HW PWM ch0 / PCM CLK"),
    (13, 27,   "GPIO27", "GPIO",  "General-purpose I/O"),
    (14, None, "GND",    "GND",   "Ground"),
    (15, 22,   "GPIO22", "GPIO",  "General-purpose I/O"),
    (16, 23,   "GPIO23", "GPIO",  "General-purpose I/O"),
    (17, None, "3V3",    "PWR33", "3.3V power rail"),
    (18, 24,   "GPIO24", "GPIO",  "General-purpose I/O"),
    (19, 10,   "MOSI",   "SPI",   "SPI0 MOSI"),
    (20, None, "GND",    "GND",   "Ground"),
    (21,  9,   "MISO",   "SPI",   "SPI0 MISO"),
    (22, 25,   "GPIO25", "GPIO",  "General-purpose I/O"),
    (23, 11,   "SCLK",   "SPI",   "SPI0 clock"),
    (24,  8,   "CE0",    "SPI",   "SPI0 chip enable 0 (active LOW)"),
    (25, None, "GND",    "GND",   "Ground"),
    (26,  7,   "CE1",    "SPI",   "SPI0 chip enable 1 (active LOW)"),
    (27, None, "ID_SD",  "ID",    "HAT EEPROM SDA — reserved"),
    (28, None, "ID_SC",  "ID",    "HAT EEPROM SCL — reserved"),
    (29,  5,   "GPIO5",  "GPIO",  "GPIO — pull-up on boot"),
    (30, None, "GND",    "GND",   "Ground"),
    (31,  6,   "GPIO6",  "GPIO",  "GPIO — pull-up on boot"),
    (32, 12,   "PWM0",   "PWM",   "HW PWM ch0 (alt pin)"),
    (33, 13,   "PWM1",   "PWM",   "HW PWM ch1"),
    (34, None, "GND",    "GND",   "Ground"),
    (35, 19,   "MISO1",  "SPI",   "SPI1 MISO / PCM FS"),
    (36, 16,   "CE0_1",  "SPI",   "SPI1 chip enable 0"),
    (37, 26,   "GPIO26", "GPIO",  "General-purpose I/O"),
    (38, 20,   "MOSI1",  "SPI",   "SPI1 MOSI / PCM DIN"),
    (39, None, "GND",    "GND",   "Ground"),
    (40, 21,   "SCLK1",  "SPI",   "SPI1 clock / PCM DOUT"),
]

# Column widths — kept short so descriptions never overflow
_CW_DESC  = 28   # description column
_CW_LABEL = 7    # label column
_CW_TYPE  = 5    # type column


def _pc(type_key: str, text: str) -> str:
    """Wrap text in the pin-type ANSI colour."""
    return f"{PIN_COLORS.get(type_key, RESET)}{text}{RESET}"

def _trunc(s: str, width: int) -> str:
    """Truncate string to exactly `width` printable chars."""
    return s[:width] if len(s) > width else s.ljust(width)

def _bcm_to_phys(bcm: int) -> str:
    for phys, b, *_ in PIN_MAP:
        if b == bcm:
            return str(phys)
    return "?"


def gpio_reference():
    W = _CW_DESC
    L = _CW_LABEL
    T = _CW_TYPE

    total_w = 3 + 2 + 4 + 2 + L + 2 + T + 2 + W + 3 + W + 2 + T + 2 + L + 2 + 4 + 2 + 3
    bar = "─" * total_w

    print(f"\n{CYAN}╔══════════════════════════════════════════════════════════╗")
    print(f"║       Raspberry Pi — 40-Pin GPIO Reference               ║")
    print(f"╚══════════════════════════════════════════════════════════╝{RESET}")
    print(f"  {DIM}Pin 1 = top-left when USB ports face down{RESET}\n")

    # Header (plain, no colour codes so widths are predictable)
    hdr_l = (f"{'PIN':>3}  {'BCM':>4}  "
             f"{'LABEL':<{L}}  {'TYPE':<{T}}  {'DESCRIPTION':<{W}}")
    hdr_r = (f"{'DESCRIPTION':<{W}}  {'TYPE':<{T}}  "
             f"{'LABEL':<{L}}  {'BCM':>4}  {'PIN':>3}")
    print(f"  {DIM}{hdr_l}   │   {hdr_r}{RESET}")
    print(f"  {DIM}{bar}{RESET}")

    for i in range(0, 40, 2):
        lp = PIN_MAP[i]
        rp = PIN_MAP[i + 1]

        l_bcm = str(lp[1]) if lp[1] is not None else "—"
        r_bcm = str(rp[1]) if rp[1] is not None else "—"

        l_label = _trunc(lp[2], L)
        r_label = _trunc(rp[2], L)
        l_type  = _trunc(lp[3], T)
        r_type  = _trunc(rp[3], T)
        l_desc  = _trunc(lp[4], W)
        r_desc  = _trunc(rp[4], W)

        left = (f"{BOLD}{lp[0]:>3}{RESET}  "
                f"{DIM}{l_bcm:>4}{RESET}  "
                f"{_pc(lp[3], l_label)}  "
                f"{_pc(lp[3], l_type)}  "
                f"{l_desc}")

        right = (f"{r_desc}  "
                 f"{_pc(rp[3], r_type)}  "
                 f"{_pc(rp[3], r_label)}  "
                 f"{DIM}{r_bcm:>4}{RESET}  "
                 f"{BOLD}{rp[0]:>3}{RESET}")

        print(f"  {left}   {DIM}│{RESET}   {right}")

    print(f"\n  {DIM}{bar}{RESET}")
    legend = "  "
    for key, label in [("PWR33","3.3V"),("PWR5","5V"),("GND","GND"),
                        ("GPIO","GPIO"),("I2C","I2C"),("SPI","SPI"),
                        ("UART","UART"),("PWM","PWM"),("ID","HAT-ID")]:
        legend += _pc(key, f"■ {label}") + "  "
    print(legend + "\n")


def gpio_reference_search():
    print(f"\n{CYAN}=== PIN SEARCH ==={RESET}")
    query = _input("  BCM number, label or keyword: ").strip().lower()
    if not query:
        return
    found = [p for p in PIN_MAP
             if query in f"{p[1]} {p[2]} {p[3]} {p[4]}".lower()]
    if not found:
        warn(f"No pins matched '{query}'"); return

    print()
    for phys, bcm, label, ptype, desc in found:
        bcm_str = f"BCM {bcm}" if bcm is not None else "no BCM"
        print(f"  {BOLD}Pin {phys:>2}{RESET}  {_pc(ptype, f'{label:<8}')}  "
              f"{DIM}{bcm_str:>7}{RESET}  {desc}")
        if bcm is not None:
            print(f"         {DIM}Python:  GPIO.setup({bcm}, GPIO.OUT) "
                  f"/ GPIO.setup({bcm}, GPIO.IN){RESET}")
    print()


def gpio_reference_by_type():
    print(f"\n{CYAN}=== FILTER BY TYPE ==={RESET}")
    print("  gpio  i2c  spi  uart  pwm  pwr  gnd  id  (ENTER = all)")
    choice = _input("  Type: ").strip().upper()
    type_map = {"PWR": ["PWR33", "PWR5"], "": None}
    filter_types = type_map.get(choice, [choice])

    print()
    for phys, bcm, label, ptype, desc in PIN_MAP:
        if filter_types and ptype not in filter_types:
            continue
        bcm_str = f"BCM{bcm:>3}" if bcm is not None else "      "
        print(f"  {BOLD}Pin {phys:>2}{RESET}  {_pc(ptype, f'{label:<8}')}  "
              f"{DIM}{bcm_str}{RESET}  {desc}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# DEPENDENCY CHECK
# ══════════════════════════════════════════════════════════════════════════════
def check_deps(silent: bool = False) -> bool:
    if not silent:
        print(f"\n{CYAN}=== DEPENDENCY CHECK ==={RESET}")

    py_libs = [
        ("RPi.GPIO",  "RPi.GPIO",  "pip install RPi.GPIO"),
        ("pigpio",    "pigpio",    "pip install pigpio  +  sudo systemctl start pigpiod"),
        ("lgpio",     "lgpio",     "pip install lgpio"),
        ("gpiod",     "gpiod",     "pip install gpiod"),
        ("gpiozero",  "gpiozero",  "pip install gpiozero"),
        ("spidev",    "spidev",    "pip install spidev"),
        ("pyserial",  "serial",    "pip install pyserial"),
        ("smbus2",    "smbus2",    "pip install smbus2"),
        ("busio",     "busio",     "pip install adafruit-blinka"),
    ]
    sys_tools = [
        ("i2cdetect", "which i2cdetect", "sudo apt install i2c-tools"),
        ("vcgencmd",  "which vcgencmd",  "sudo apt install libraspberrypi-bin"),
    ]

    all_ok = True
    for name, module, hint in py_libs:
        try:
            __import__(module)
            if not silent: ok(f"{name}")
        except ImportError:
            if not silent: warn(f"{name} not installed  →  {hint}")

    for name, cmd, hint in sys_tools:
        if os.system(f"{cmd} > /dev/null 2>&1") == 0:
            if not silent: ok(f"{name} available")
        else:
            if not silent: warn(f"{name} missing  →  {hint}")
            all_ok = False

    if not silent:
        print(f"\n  Active GPIO backend : {BOLD}{GPIO_ADAPTER.name}{RESET}")
        print(f"  Override via env    : "
              f"RPI_GPIO_BACKEND={{rpigpio|pigpio|lgpio|gpiod|gpiozero}}\n")
    return all_ok


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM INFO
# ══════════════════════════════════════════════════════════════════════════════
def system_info():
    print(f"\n{CYAN}=== SYSTEM INFO ==={RESET}")

    def _read(path, default="unavailable"):
        try:
            with open(path) as f: return f.read().strip()
        except OSError: return default

    def _cmd(cmd, default="unavailable"):
        try:
            return subprocess.check_output(
                cmd, shell=True, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception: return default

    model    = _read("/proc/device-tree/model").replace("\x00", "")
    serial   = next((l.split(":")[1].strip()
                     for l in _read("/proc/cpuinfo").splitlines()
                     if l.startswith("Serial")), "unknown")
    mem_kb   = _read("/proc/meminfo").splitlines()[0].split()[1]
    temp     = _cmd("vcgencmd measure_temp").replace("temp=", "")
    throttle = _cmd("vcgencmd get_throttled")
    kernel   = _cmd("uname -r")
    uptime   = _cmd("uptime -p")
    os_name  = _cmd("cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2").strip('"')

    rows = [
        ("Model",      model),
        ("Serial",     serial),
        ("OS",         os_name),
        ("Kernel",     kernel),
        ("Uptime",     uptime),
        ("RAM",        f"{int(mem_kb)//1024} MB"),
        ("CPU temp",   temp),
        ("Throttled",  throttle),
        ("GPIO back",  GPIO_ADAPTER.name),
    ]
    for label, value in rows:
        print(f"  {DIM}{label:<12}{RESET}{value}")

    if throttle not in ("throttled=0x0", "unavailable"):
        warn("Throttling detected — check your power supply / cooling")
    log.info(f"System info: model={model} temp={temp} throttle={throttle}")


# ══════════════════════════════════════════════════════════════════════════════
# GPIO PIN SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════════
def gpio_snapshot():
    print(f"\n{CYAN}=== GPIO PIN STATE SNAPSHOT ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return

    bcm_pins = [p[1] for p in PIN_MAP if p[1] is not None]
    states   = {}
    try:
        for pin in bcm_pins:
            GPIO_ADAPTER.setup(pin, "in")
            states[pin] = GPIO_ADAPTER.input(pin)
    finally:
        GPIO_ADAPTER._release_pins(*bcm_pins)

    print(f"  {DIM}{'BCM':<8}{'Phys':<6}{'State'}{RESET}")
    for pin in sorted(states):
        phys  = _bcm_to_phys(pin)
        level = states[pin]
        color = GREEN if level else DIM
        label = "HIGH" if level else "LOW"
        print(f"  {color}GPIO{pin:<4}  Pin {phys:<4}  {label}{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# PIN CONFLICT GUARD
# ══════════════════════════════════════════════════════════════════════════════
def _warn_if_pin_in_use(bcm: int):
    sysfs = f"/sys/class/gpio/gpio{bcm}"
    if os.path.exists(sysfs):
        warn(f"GPIO{bcm} appears already exported ({sysfs}) — may conflict")


# ══════════════════════════════════════════════════════════════════════════════
# GPIO LOOPBACK TEST
# ══════════════════════════════════════════════════════════════════════════════
def gpio_test() -> bool:
    print(f"\n{CYAN}=== GPIO LOOPBACK TEST ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return False

    pairs = [tuple(p) for p in CFG["gpio_pairs"]]
    info("Wire these pairs with jumper wires:")
    for o, i in pairs:
        info(f"  GPIO{o} (Pin {_bcm_to_phys(o)}) ↔ GPIO{i} (Pin {_bcm_to_phys(i)})")
    _input("  Press ENTER when ready… ")

    results  = []
    all_pins = [p for pair in pairs for p in pair]
    try:
        for out_pin, in_pin in pairs:
            _warn_if_pin_in_use(out_pin)
            GPIO_ADAPTER.setup(out_pin, "out")
            GPIO_ADAPTER.setup(in_pin,  "in")
            for level, label in [(1, "HIGH"), (0, "LOW")]:
                GPIO_ADAPTER.output(out_pin, level)
                time.sleep(0.05)
                try:
                    read = GPIO_ADAPTER.input(in_pin)
                except PermissionError:
                    fail(f"PermissionError on GPIO{in_pin} — "
                         f"sudo usermod -aG gpio $USER  then re-login")
                    results.append(False); continue
                passed = read == level
                msg    = f"GPIO{out_pin}→GPIO{in_pin}  {label}"
                (ok if passed else fail)(msg)
                log.info(f"GPIO loopback {msg}: {'PASS' if passed else 'FAIL'}")
                results.append(passed)
    finally:
        GPIO_ADAPTER._release_pins(*all_pins)
    return all(results)


# ══════════════════════════════════════════════════════════════════════════════
# GPIO INTERRUPT TEST
# ══════════════════════════════════════════════════════════════════════════════
def gpio_interrupt_test() -> bool:
    print(f"\n{CYAN}=== GPIO INTERRUPT / EDGE DETECT TEST ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return False

    if GPIO_ADAPTER.name not in ("RPi.GPIO",):
        warn(f"add_event_detect not supported by {GPIO_ADAPTER.name} — polling fallback")
        return _gpio_interrupt_poll_fallback()

    out_pin, in_pin = 17, 27
    info(f"Wire GPIO{out_pin} (Pin {_bcm_to_phys(out_pin)}) → "
         f"GPIO{in_pin} (Pin {_bcm_to_phys(in_pin)})")
    _input("  Press ENTER when ready… ")

    detected = []
    try:
        GPIO_ADAPTER.setup(out_pin, "out")
        GPIO_ADAPTER.setup(in_pin,  "in")
        GPIO_ADAPTER.output(out_pin, 0)
        GPIO_ADAPTER.add_event_detect(in_pin, "rising",
                                      callback=lambda _: detected.append(1))
        for _ in range(5):
            GPIO_ADAPTER.output(out_pin, 1); time.sleep(0.05)
            GPIO_ADAPTER.output(out_pin, 0); time.sleep(0.05)
        time.sleep(0.15)
        passed = len(detected) >= 4
        msg    = f"Detected {len(detected)}/5 rising edges"
        (ok if passed else fail)(msg)
        log.info(f"IRQ test: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    except AttributeError:
        warn("Backend missing add_event_detect — using poll fallback")
        return _gpio_interrupt_poll_fallback()
    except Exception as e:
        fail(f"Interrupt error: {e}"); return False
    finally:
        GPIO_ADAPTER._release_pins(out_pin, in_pin)


def _gpio_interrupt_poll_fallback() -> bool:
    out_pin, in_pin = 17, 27
    info("Polling-based edge detection (fallback)")
    _input("  Press ENTER when ready… ")
    edges = 0; last = 0
    try:
        GPIO_ADAPTER.setup(out_pin, "out")
        GPIO_ADAPTER.setup(in_pin,  "in")
        GPIO_ADAPTER.output(out_pin, 0)
        deadline = time.time() + 2.0; pulse = 0
        while time.time() < deadline:
            pulse = 1 - pulse
            GPIO_ADAPTER.output(out_pin, pulse)
            time.sleep(0.05)
            cur = GPIO_ADAPTER.input(in_pin)
            if cur == 1 and last == 0: edges += 1
            last = cur
        passed = edges >= 4
        msg    = f"Polling detected {edges} rising edges"
        (ok if passed else fail)(msg)
        log.info(f"IRQ poll fallback: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    finally:
        GPIO_ADAPTER._release_pins(out_pin, in_pin)


# ══════════════════════════════════════════════════════════════════════════════
# PWM — LED RAMP
# ══════════════════════════════════════════════════════════════════════════════
def pwm_test() -> bool:
    print(f"\n{CYAN}=== PWM TEST (LED BRIGHTNESS RAMP) ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return False

    pin  = CFG["pwm_pin"]
    freq = CFG["pwm_freq_hz"]
    info(f"LED + 330Ω → GPIO{pin} (Pin {_bcm_to_phys(pin)}), cathode → GND")
    _input("  Press ENTER to start… ")
    try:
        pwm = GPIO_ADAPTER.pwm(pin, freq)
        pwm.start(0)
        for dc in list(range(0, 101, 5)) + list(range(100, -1, -5)):
            print(f"\r  Duty cycle: {dc:3d}%", end="", flush=True)
            pwm.ChangeDutyCycle(dc)
            time.sleep(0.08)
        print()
        pwm.stop()
        ok("PWM ramp completed")
        log.info("PWM LED test: PASS")
        return True
    except Exception as e:
        fail(f"PWM error: {e}"); log.error(f"PWM: {e}"); return False
    finally:
        GPIO_ADAPTER._release_pins(pin)


# ══════════════════════════════════════════════════════════════════════════════
# PWM — SERVO SWEEP
# ══════════════════════════════════════════════════════════════════════════════
def pwm_servo_test() -> bool:
    print(f"\n{CYAN}=== SERVO PWM TEST ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return False

    pin = CFG["servo_pin"]
    info(f"Servo signal → GPIO{pin} (Pin {_bcm_to_phys(pin)})")
    info("Servo VCC    → 5V (Pin 2 or 4),  GND → Pin 6")
    info("Sweep: 0° → 90° → 180° → 90° → 0°")
    _input("  Press ENTER to start… ")

    if GPIO_ADAPTER.name == "pigpio" and hasattr(GPIO_ADAPTER, "servo_pulse"):
        return _servo_pigpio(pin)

    try:
        pwm = GPIO_ADAPTER.pwm(pin, 50)  # 50 Hz standard for servos
        pwm.start(7.5)
        positions = [(2.5,"  0°"), (7.5," 90°"), (12.5,"180°"),
                     (7.5," 90°"), (2.5,"  0°")]
        for dc, label in positions:
            print(f"\r  Position: {label}", end="", flush=True)
            pwm.ChangeDutyCycle(dc)
            time.sleep(0.7)
        print()
        pwm.stop()
        ok("Servo sweep completed")
        log.info("Servo PWM: PASS")
        return True
    except Exception as e:
        fail(f"Servo error: {e}"); log.error(f"Servo: {e}"); return False
    finally:
        GPIO_ADAPTER._release_pins(pin)


def _servo_pigpio(pin: int) -> bool:
    try:
        positions = [(500,"  0°"),(1500," 90°"),(2500,"180°"),
                     (1500," 90°"),(500,"  0°")]
        for us, label in positions:
            print(f"\r  Position: {label}  ({us}µs)", end="", flush=True)
            GPIO_ADAPTER.servo_pulse(pin, us)
            time.sleep(0.7)
        GPIO_ADAPTER.servo_pulse(pin, 0)  # release torque
        print()
        ok("Servo sweep completed (pigpio native)")
        log.info("Servo PWM pigpio: PASS")
        return True
    except Exception as e:
        fail(f"pigpio servo error: {e}"); return False
    finally:
        GPIO_ADAPTER._release_pins(pin)


# ══════════════════════════════════════════════════════════════════════════════
# I2C — primary + alternatives
# ══════════════════════════════════════════════════════════════════════════════
def i2c_test() -> bool:
    print(f"\n{CYAN}=== I2C BUS SCAN ==={RESET}")
    info(f"SDA → Pin 3  |  SCL → Pin 5  |  Bus {CFG['i2c_bus']}")
    info("Enable I2C: sudo raspi-config → Interface Options → I2C")
    _input("  Press ENTER to scan… ")

    ret = os.system(f"i2cdetect -y {CFG['i2c_bus']}")
    if ret == 0:
        ok("i2cdetect scan succeeded")
        log.info("I2C scan (i2cdetect): PASS")
        return True

    warn("i2cdetect failed — trying smbus2 fallback")
    return _i2c_smbus2()


def _i2c_smbus2() -> bool:
    """Alternative I2C scan using smbus2 (pure Python)."""
    print(f"\n{CYAN}  → I2C via smbus2{RESET}")
    try:
        import smbus2
    except ImportError:
        fail("smbus2 not installed  →  pip install smbus2"); return False
    try:
        bus   = smbus2.SMBus(CFG["i2c_bus"])
        found = []
        for addr in range(0x03, 0x78):
            try:
                bus.read_byte(addr); found.append(hex(addr))
            except OSError:
                pass
        bus.close()
        if found:
            ok(f"smbus2 found: {', '.join(found)}")
        else:
            info("smbus2: No devices (bus OK)")
        log.info(f"I2C smbus2: {found}")
        return True
    except Exception as e:
        fail(f"smbus2 error: {e}"); log.error(f"I2C smbus2: {e}"); return False


def _i2c_busio() -> bool:
    """Alternative I2C using CircuitPython busio (Blinka)."""
    print(f"\n{CYAN}=== I2C SCAN (busio / CircuitPython) ==={RESET}")
    try:
        import board, busio
    except ImportError:
        fail("busio not available  →  pip install adafruit-blinka"); return False
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        while not i2c.try_lock(): time.sleep(0.01)
        addrs = [hex(a) for a in i2c.scan()]
        i2c.unlock(); i2c.deinit()
        if addrs:
            ok(f"busio found: {', '.join(addrs)}")
        else:
            info("busio: No devices (bus OK)")
        log.info(f"I2C busio: {addrs}")
        return True
    except Exception as e:
        fail(f"busio error: {e}"); log.error(f"I2C busio: {e}"); return False


def i2c_alt_menu():
    print(f"\n{CYAN}=== I2C ALTERNATIVES ==={RESET}")
    print("  1. smbus2 scan (pure Python)")
    print("  2. busio  scan (CircuitPython / Blinka)")
    print("  0. Back")
    choice = _input("\n  Select: ").strip()
    if choice == "1": _i2c_smbus2()
    elif choice == "2": _i2c_busio()


# ══════════════════════════════════════════════════════════════════════════════
# SPI — spidev + busio fallback
# ══════════════════════════════════════════════════════════════════════════════
def spi_test() -> bool:
    print(f"\n{CYAN}=== SPI LOOPBACK TEST ==={RESET}")
    info("Connect MOSI (Pin 19) ↔ MISO (Pin 21)")
    info("Enable SPI: sudo raspi-config → Interface Options → SPI")
    _input("  Press ENTER to run… ")
    try:
        import spidev
    except ImportError:
        warn("pip install spidev")
        return _spi_busio_fallback()
    try:
        spi = spidev.SpiDev()
        spi.open(CFG["spi_bus"], CFG["spi_device"])
        spi.max_speed_hz = CFG["spi_speed_hz"]
        spi.mode = 0
        payload  = [0xDE, 0xAD, 0xBE, 0xEF]
        received = spi.xfer2(payload)
        spi.close()
        passed = payload == received
        msg    = f"Sent {[hex(b) for b in payload]} | Got {[hex(b) for b in received]}"
        (ok if passed else fail)(msg)
        log.info(f"SPI spidev: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    except Exception as e:
        fail(f"SPI spidev error: {e}"); log.error(f"SPI spidev: {e}")
        warn("Trying busio fallback…")
        return _spi_busio_fallback()


def _spi_busio_fallback() -> bool:
    print(f"\n{CYAN}  → SPI via busio (Blinka){RESET}")
    try:
        import board, busio
    except ImportError:
        fail("busio not available  →  pip install adafruit-blinka"); return False
    try:
        spi = busio.SPI(board.SCLK, MOSI=board.MOSI, MISO=board.MISO)
        while not spi.try_lock(): time.sleep(0.01)
        spi.configure(baudrate=500000, polarity=0, phase=0, bits=8)
        payload  = bytearray([0xDE, 0xAD, 0xBE, 0xEF])
        received = bytearray(4)
        spi.write_readinto(payload, received)
        spi.unlock(); spi.deinit()
        passed = list(payload) == list(received)
        msg    = f"Sent {list(payload)} | Got {list(received)}"
        (ok if passed else fail)(msg)
        log.info(f"SPI busio: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    except Exception as e:
        fail(f"busio SPI error: {e}"); log.error(f"SPI busio: {e}"); return False


# ══════════════════════════════════════════════════════════════════════════════
# UART — pyserial + pigpio bit-bang fallback
# ══════════════════════════════════════════════════════════════════════════════
def uart_test() -> bool:
    print(f"\n{CYAN}=== UART LOOPBACK TEST ==={RESET}")
    info("Connect TX (Pin 8) ↔ RX (Pin 10)")
    info("Disable serial console first:")
    info("  sudo raspi-config → Interface → Serial Port")
    info("    → login shell: No  → serial port hardware: Yes")
    _input("  Press ENTER to run… ")
    try:
        import serial
    except ImportError:
        warn("pip install pyserial")
        return _uart_pigpio_fallback()

    device   = CFG["uart_device"]
    baudrate = CFG["uart_baud"]
    test_msg = b"UART_LOOPBACK_TEST_OK"
    try:
        with serial.Serial(device, baudrate=baudrate, timeout=2) as ser:
            ser.reset_input_buffer()
            ser.write(test_msg)
            response = b""
            deadline = time.time() + 2.0
            while len(response) < len(test_msg) and time.time() < deadline:
                chunk = ser.read(len(test_msg) - len(response))
                if chunk: response += chunk
                else:     time.sleep(0.05)
        passed = response == test_msg
        msg    = f"Sent: {test_msg!r}  |  Got: {response!r}"
        (ok if passed else fail)(msg)
        log.info(f"UART pyserial: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    except Exception as e:
        fail(f"UART error: {e}"); log.error(f"UART pyserial: {e}")
        warn("Trying pigpio bit-bang fallback…")
        return _uart_pigpio_fallback()


def _uart_pigpio_fallback() -> bool:
    print(f"\n{CYAN}  → UART via pigpio bit-bang{RESET}")
    if GPIO_ADAPTER.name != "pigpio":
        fail("pigpio backend not active — cannot use bit-bang UART"); return False
    try:
        pi       = GPIO_ADAPTER._pi
        pg       = GPIO_ADAPTER._pg
        tx_pin   = 14; rx_pin = 15
        baud     = CFG["uart_baud"]
        test_msg = b"UART_BB_OK"

        pi.set_mode(tx_pin, pg.OUTPUT)
        pi.bb_serial_read_open(rx_pin, baud, 8)
        pi.wave_clear()
        pi.wave_add_serial(tx_pin, baud, test_msg)
        wave_id = pi.wave_create()
        pi.wave_send_once(wave_id)
        while pi.wave_tx_busy(): time.sleep(0.01)
        time.sleep(0.2)
        count, data = pi.bb_serial_read(rx_pin)
        pi.bb_serial_read_close(rx_pin)
        pi.wave_delete(wave_id)

        passed = data == test_msg
        msg    = f"Sent: {test_msg!r}  |  Got: {data!r}"
        (ok if passed else fail)(msg)
        log.info(f"UART pigpio: {'PASS' if passed else 'FAIL'} — {msg}")
        return passed
    except Exception as e:
        fail(f"pigpio UART error: {e}"); log.error(f"UART pigpio: {e}"); return False


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL GPIO CONTROL
# ══════════════════════════════════════════════════════════════════════════════
def manual_gpio():
    print(f"\n{CYAN}=== MANUAL GPIO CONTROL ==={RESET}")
    if not HAS_GPIO:
        fail("No GPIO backend available"); return
    pin = None
    try:
        bcm  = int(_input("  BCM GPIO number: "))
        phys = _bcm_to_phys(bcm)
        info(f"GPIO{bcm} → physical Pin {phys}")
        mode = _input("  Mode (out/in): ").strip().lower()
        pin  = bcm
        if mode == "out":
            GPIO_ADAPTER.setup(bcm, "out")
            val = int(_input("  Value (1=HIGH, 0=LOW): "))
            GPIO_ADAPTER.output(bcm, val)
            ok(f"GPIO{bcm} (Pin {phys}) → {'HIGH' if val else 'LOW'}")
            log.info(f"Manual GPIO{bcm} = {val}")
        elif mode == "in":
            GPIO_ADAPTER.setup(bcm, "in")
            val = GPIO_ADAPTER.input(bcm)
            ok(f"GPIO{bcm} (Pin {phys}) reads {'HIGH' if val else 'LOW'} ({val})")
            log.info(f"Manual GPIO{bcm} read {val}")
        else:
            warn("Use 'in' or 'out'")
    except PermissionError:
        fail("PermissionError — sudo usermod -aG gpio $USER  then re-login")
    except (ValueError, KeyboardInterrupt):
        fail("Invalid input")
    finally:
        if pin is not None:
            GPIO_ADAPTER._release_pins(pin)


# ══════════════════════════════════════════════════════════════════════════════
# SESSION LOG VIEWER
# ══════════════════════════════════════════════════════════════════════════════
def view_log(lines: int = 40):
    print(f"\n{CYAN}=== SESSION LOG — last {lines} lines ==={RESET}")
    try:
        with open("rpi_test.log") as f:
            tail = f.readlines()[-lines:]
        for line in tail:
            line  = line.rstrip()
            color = GREEN if "PASS" in line else RED if "FAIL" in line else DIM
            print(f"  {color}{line}{RESET}")
    except FileNotFoundError:
        warn("No log file yet — run some tests first")


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND INFO
# ══════════════════════════════════════════════════════════════════════════════
def show_backends():
    print(f"\n{CYAN}=== GPIO BACKEND REFERENCE ==={RESET}")
    backends = [
        ("RPi.GPIO",  "rpigpio",
         "pip install RPi.GPIO",
         "Default for Pi 1–4. Not supported on Pi 5.",
         "GPIO.setup(), GPIO.output(), GPIO.PWM()"),
        ("pigpio",    "pigpio",
         "pip install pigpio  +  sudo systemctl start pigpiod",
         "Hardware-timed PWM, servo pulses, remote GPIO. Best accuracy.",
         "pi.write(), pi.set_servo_pulsewidth()"),
        ("lgpio",     "lgpio",
         "pip install lgpio",
         "Modern replacement for RPi.GPIO. Works on Pi 5.",
         "lgpio.gpio_write(), lgpio.tx_pwm()"),
        ("gpiod",     "gpiod",
         "pip install gpiod  (libgpiod >= 2.x)",
         "Kernel char-device interface. No root needed. Software PWM only.",
         "chip.get_line(), line.set_value()"),
        ("gpiozero",  "gpiozero",
         "pip install gpiozero",
         "High-level abstraction. Great for quick scripts.",
         "LED(), Button(), PWMLED(), MCP3008()"),
        ("busio",     "—",
         "pip install adafruit-blinka",
         "CircuitPython I2C/SPI/UART abstraction (Blinka). Platform-agnostic.",
         "busio.I2C(), busio.SPI(), busio.UART()"),
        ("smbus2",    "—",
         "pip install smbus2",
         "Pure-Python SMBus/I2C. No GPIO dependency.",
         "SMBus(1).read_byte(addr), write_byte_data()"),
    ]
    for name, env_val, install, desc, api in backends:
        active = " ← active" if GPIO_ADAPTER.name == name else ""
        color  = CYAN if active else ""
        print(f"\n  {color}{BOLD}{name}{RESET}{GREEN}{active}{RESET}")
        print(f"    {DIM}Install :{RESET} {install}")
        print(f"    {DIM}Info    :{RESET} {desc}")
        print(f"    {DIM}Key API :{RESET} {api}")
        if env_val != "—":
            print(f"    {DIM}Select  :{RESET} "
                  f"RPI_GPIO_BACKEND={env_val} python3 rpi_hardware_test.py")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG EDITOR
# ══════════════════════════════════════════════════════════════════════════════
def edit_config():
    print(f"\n{CYAN}=== CONFIGURATION ==={RESET}")
    print(f"  Config file: {CONFIG_FILE}")
    for k, v in CFG.items():
        print(f"  {DIM}{k:<20}{RESET}{v}")
    print()
    choice = _input("  Save defaults to file? (y/n): ").strip().lower()
    if choice == "y":
        save_default_config()
    else:
        info("Edit rpi_test.json manually to customise pins, speeds, device paths")


# ══════════════════════════════════════════════════════════════════════════════
# RUN ALL + SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
def run_all() -> dict:
    results = {
        "GPIO Loopback":  gpio_test(),
        "GPIO Interrupt": gpio_interrupt_test(),
        "PWM (LED)":      pwm_test(),
        "Servo PWM":      pwm_servo_test(),
        "I2C Scan":       i2c_test(),
        "SPI Loopback":   spi_test(),
        "UART Loopback":  uart_test(),
    }
    _print_summary(results)
    log.info(f"Run-all: {results}")
    return results


def _print_summary(results: dict):
    total  = len(results)
    passed = sum(results.values())
    bar    = "─" * 36
    print(f"\n{CYAN}{bar}{RESET}")
    print(f"{CYAN}  TEST SUMMARY{RESET}")
    print(f"{CYAN}{bar}{RESET}")
    for name, p in results.items():
        color = GREEN if p else RED
        state = "PASS" if p else "FAIL"
        print(f"  {color}{state}{RESET}  {name}")
    color = GREEN if passed == total else (YELLOW if passed > 0 else RED)
    print(f"\n  {color}{passed}/{total} tests passed{RESET}")
    print(f"{CYAN}{bar}{RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
# HEADLESS / CI
# ══════════════════════════════════════════════════════════════════════════════
def run_headless():
    info("Headless mode — all prompts auto-confirmed")
    results = run_all()
    passed  = sum(results.values())
    sys.exit(0 if passed == len(results) else 1)


# ══════════════════════════════════════════════════════════════════════════════
# MENU
# ══════════════════════════════════════════════════════════════════════════════
def menu():
    options = {
        "1":   ("GPIO Loopback Test",         gpio_test),
        "1b":  ("GPIO Interrupt / Edge Test",  gpio_interrupt_test),
        "2":   ("Manual GPIO Control",         manual_gpio),
        "3":   ("PWM Test  (LED ramp)",        pwm_test),
        "3b":  ("PWM Test  (Servo sweep)",     pwm_servo_test),
        "4":   ("I2C Scan  (i2cdetect)",       i2c_test),
        "4b":  ("I2C Alternatives",            i2c_alt_menu),
        "5":   ("SPI Loopback Test",           spi_test),
        "6":   ("UART Loopback Test",          uart_test),
        "7":   ("Run ALL Tests",               run_all),
        "s":   ("System Info",                 system_info),
        "p":   ("GPIO Pin Snapshot",           gpio_snapshot),
        "c":   ("Check Dependencies",          check_deps),
        "b":   ("GPIO Backend Reference",      show_backends),
        "l":   ("View Session Log",            view_log),
        "cfg": ("Edit / Save Config",          edit_config),
        "8":   ("40-Pin Reference Table",      gpio_reference),
        "9":   ("Search Pins",                 gpio_reference_search),
        "f":   ("Filter Pins by Type",         gpio_reference_by_type),
        "0":   ("Exit",                        None),
    }
    REF_KEYS  = {"8", "9", "f"}
    INFO_KEYS = {"s", "p", "c", "b", "l", "cfg"}

    while True:
        print(f"\n{CYAN}╔════════════════════════════════╗")
        print(f"║  Raspberry Pi Hardware Tests   ║")
        print(f"╚════════════════════════════════╝{RESET}")
        print(f"  {DIM}Backend: {GPIO_ADAPTER.name}{RESET}\n")

        sections = {
            "1":   f"  {DIM}──── Hardware Tests ──────────────{RESET}",
            "s":   f"  {DIM}──── Diagnostics ─────────────────{RESET}",
            "8":   f"  {DIM}──── GPIO Reference ──────────────{RESET}",
        }
        for key, (label, _) in options.items():
            if key in sections:
                print(sections[key])
            tag = (f"{CYAN}[ref]{RESET}" if key in REF_KEYS else
                   f"{YELLOW}[inf]{RESET}" if key in INFO_KEYS else "     ")
            print(f"  {tag}  {key:>3}.  {label}")

        choice = _input("\nSelect option: ").strip().lower()
        if choice == "0":
            info("Bye!")
            GPIO_ADAPTER.cleanup()
            break
        elif choice in options:
            _, fn = options[choice]
            if fn:
                try:
                    fn()
                except KeyboardInterrupt:
                    warn("Interrupted")
        else:
            warn("Invalid choice — try again")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if "--headless"   in sys.argv: run_headless()
    elif "--check"    in sys.argv: check_deps()
    elif "--info"     in sys.argv: system_info()
    elif "--backends" in sys.argv: show_backends()
    elif "--snapshot" in sys.argv: gpio_snapshot()
    elif "--save-config" in sys.argv: save_default_config()
    else:
        check_deps(silent=True)
        menu()
