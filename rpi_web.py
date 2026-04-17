#!/usr/bin/env python3
"""
rpi_web.py — Raspberry Pi Hardware Test Suite Web UI  v2.1
Dark/light theme, alt-function table, live temp widget,
GPIO pin toggles, test history, WebSocket-style SSE, JSON results.

Usage:
    sudo python3 rpi_web.py              # http://0.0.0.0:5000
    sudo python3 rpi_web.py --port 8080

Find Pi IP:  hostname -I
"""

import io, os, re, sys, json, time, queue, threading, subprocess, datetime
from contextlib import redirect_stdout

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    print("Flask not installed.  Run:  pip install flask")
    sys.exit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import rpi_hardware_test as core
except ImportError:
    print("rpi_hardware_test.py not found — place it beside rpi_web.py")
    sys.exit(1)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)

_test_lock   = threading.Lock()
_test_active = threading.Event()
_test_history = []  # list of {name, timestamp, results}

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def _strip_ansi(s): return _ANSI_RE.sub("", s)

def _classify(line):
    c = line.strip()
    if "✔ PASS" in c or ("PASS" in c and "[INFO]" in c): return "pass"
    if "✖ FAIL" in c or ("FAIL" in c and "[INFO]" in c): return "fail"
    if c.startswith("⚠") or "warn" in c.lower(): return "warn"
    if c.startswith("ℹ"): return "info"
    if any(c.startswith(x) for x in ("═","╔","╚","╠","┌","└","─","══")): return "header"
    return "normal"

class _QueueWriter(io.TextIOBase):
    def __init__(self, q): self._q = q
    def write(self, s):
        if s: self._q.put(s)
        return len(s)
    def flush(self): pass

def _run_and_stream(fn, test_name, *args):
    q = queue.Queue()
    sentinel = object()
    output_lines = []

    def _worker():
        writer = _QueueWriter(q)
        try:
            with redirect_stdout(writer):
                result = fn(*args)
                q.put(f"__RESULT__:{json.dumps(result) if isinstance(result, (dict,bool)) else str(result)}")
        except Exception as e:
            q.put(f"\n  ✖ FAIL  Exception: {e}\n")
        finally:
            q.put(sentinel)

    threading.Thread(target=_worker, daemon=True).start()
    buf = ""
    while True:
        try: chunk = q.get(timeout=60)
        except queue.Empty:
            yield "data: [TIMEOUT]\n\n"; break
        if chunk is sentinel:
            # Save to history
            _save_history(test_name, output_lines)
            yield "data: [DONE]\n\n"; break
        if str(chunk).startswith("__RESULT__:"):
            continue
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            clean = _strip_ansi(line)
            if clean.strip():
                output_lines.append({"line": clean, "cls": _classify(clean)})
                yield f"data: {json.dumps({'line': clean, 'cls': _classify(clean)})}\n\n"
    if buf.strip():
        clean = _strip_ansi(buf)
        yield f"data: {json.dumps({'line': clean, 'cls': _classify(clean)})}\n\n"
    _test_active.clear()

def _save_history(name, lines):
    passed = sum(1 for l in lines if l["cls"]=="pass")
    failed = sum(1 for l in lines if l["cls"]=="fail")
    _test_history.append({
        "name": name,
        "timestamp": datetime.datetime.now().isoformat(),
        "passed": passed,
        "failed": failed,
        "lines": lines[-50:],  # keep last 50 lines per run
    })
    if len(_test_history) > 100:
        _test_history.pop(0)

def _stream(fn, test_name="test"):
    def gen():
        if not _test_lock.acquire(blocking=False):
            yield 'data: {"line":"⚠ Another test is running — please wait","cls":"warn"}\n\n'
            yield "data: [DONE]\n\n"; return
        _test_active.set()
        try:
            yield from _run_and_stream(fn, test_name)
        finally:
            try: _test_lock.release()
            except RuntimeError: pass
    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"}
    )

# ── Test Routes ────────────────────────────────────────────────────────────────
@app.route("/api/test/gpio")      
def api_gpio():       return _stream(core.gpio_test,          "GPIO Loopback")
@app.route("/api/test/gpio_pull") 
def api_gpio_pull():  return _stream(core.gpio_pull_test,     "GPIO Pull R/R")
@app.route("/api/test/gpio_irq")  
def api_gpio_irq():   return _stream(core.gpio_interrupt_test,"GPIO Interrupt")
@app.route("/api/test/pwm")       
def api_pwm():        return _stream(core.pwm_test,           "PWM LED Ramp")
@app.route("/api/test/servo")     
def api_servo():      return _stream(core.pwm_servo_test,     "Servo Sweep")
@app.route("/api/test/i2c")       
def api_i2c():        return _stream(core.i2c_test,           "I2C Scan")
@app.route("/api/test/spi")       
def api_spi():        return _stream(core.spi_test,           "SPI Loopback")
@app.route("/api/test/uart")      
def api_uart():       return _stream(core.uart_test,          "UART Loopback")
@app.route("/api/test/onewire")   
def api_onewire():    return _stream(core.onewire_test,       "1-Wire DS18B20")
@app.route("/api/test/all")       
def api_all():        return _stream(core.run_all,            "Run All")
@app.route("/api/test/deps")      
def api_deps():       return _stream(core.check_deps,         "Dep Check")
@app.route("/api/test/sysinfo")   
def api_sysinfo():    return _stream(core.system_info,        "System Info")
@app.route("/api/test/snapshot")  
def api_snapshot():   return _stream(core.gpio_snapshot,      "Pin Snapshot")
@app.route("/api/test/tempmon")   
def api_tempmon():    return _stream(core.temperature_monitor,"Temp Monitor")

# ── GPIO Control ───────────────────────────────────────────────────────────────
@app.route("/api/gpio/set", methods=["POST"])
def api_gpio_set():
    d = request.get_json(force=True)
    bcm = int(d.get("pin",-1)); val = int(d.get("value",0))
    if not (0 <= bcm <= 27): return jsonify(ok=False, msg="Invalid BCM pin")
    try:
        core.GPIO_ADAPTER.setup(bcm, "out")
        core.GPIO_ADAPTER.output(bcm, val)
        return jsonify(ok=True, msg=f"GPIO{bcm} → {'HIGH' if val else 'LOW'}", value=val)
    except Exception as e:
        return jsonify(ok=False, msg=str(e))

@app.route("/api/gpio/read", methods=["POST"])
def api_gpio_read():
    d = request.get_json(force=True)
    bcm = int(d.get("pin",-1))
    if not (0 <= bcm <= 27): return jsonify(ok=False, msg="Invalid BCM pin")
    try:
        core.GPIO_ADAPTER.setup(bcm, "in")
        val = core.GPIO_ADAPTER.input(bcm)
        return jsonify(ok=True, value=val, label="HIGH" if val else "LOW")
    except Exception as e:
        return jsonify(ok=False, msg=str(e))

# ── Data Routes ────────────────────────────────────────────────────────────────
@app.route("/api/pinmap")
def api_pinmap():
    return jsonify(pins=[
        {"phys":e[0],"bcm":e[1],"label":e[2],"type":e[3],"desc":e[4],"alts":e[5]}
        for e in core.PIN_MAP
    ])

@app.route("/api/config", methods=["GET"])
def api_config_get(): return jsonify(core.CFG)

@app.route("/api/config", methods=["POST"])
def api_config_set():
    for k,v in request.get_json(force=True).items():
        if k in core.CFG: core.CFG[k] = v
    with open(core.CONFIG_FILE,"w") as f: json.dump(core.CFG, f, indent=2)
    return jsonify(ok=True, config=core.CFG)

@app.route("/api/log")
def api_log():
    lines = int(request.args.get("lines",60))
    try:
        with open(core._LOG_FILE) as f:
            tail = f.readlines()[-lines:]
        return jsonify(lines=[_strip_ansi(l.rstrip()) for l in tail])
    except FileNotFoundError:
        return jsonify(lines=[])

@app.route("/api/history")
def api_history():
    return jsonify(history=list(reversed(_test_history[-30:])))

@app.route("/api/temp")
def api_temp():
    """Live temperature endpoint."""
    try:
        raw = subprocess.check_output("vcgencmd measure_temp", shell=True,
                                      stderr=subprocess.DEVNULL, text=True).strip()
        temp = float(raw.replace("temp=","").replace("'C",""))
    except Exception:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read().strip()) / 1000.0
        except Exception:
            return jsonify(ok=False, temp=None)
    try:
        throttle = subprocess.check_output("vcgencmd get_throttled", shell=True,
                                           stderr=subprocess.DEVNULL, text=True).strip()
        throttled = throttle != "throttled=0x0"
    except Exception:
        throttled = False
    try:
        freq_raw = subprocess.check_output("vcgencmd measure_clock arm", shell=True,
                                           stderr=subprocess.DEVNULL, text=True).strip()
        freq = int(freq_raw.split("=")[1]) // 1_000_000
    except Exception:
        freq = None
    return jsonify(ok=True, temp=temp, throttled=throttled, freq_mhz=freq)

@app.route("/api/status")
def api_status():
    return jsonify(
        backend=core.GPIO_ADAPTER.name,
        has_gpio=core.HAS_GPIO,
        test_running=_test_active.is_set(),
        config=core.CFG,
    )

# ══════════════════════════════════════════════════════════════════════════════
# HTML PAGE
# ══════════════════════════════════════════════════════════════════════════════
HTML = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RPi Hardware Suite v2.1</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
:root[data-theme="dark"] {
  --bg:       #0d1117;
  --bg2:      #161b22;
  --bg3:      #1c2128;
  --bg4:      #21262d;
  --border:   #30363d;
  --border2:  #484f58;
  --text:     #e6edf3;
  --text2:    #8b949e;
  --text3:    #6e7681;
  --text4:    #484f58;

  --green:    #3fb950;
  --green-lt: #0d2116;
  --green-bd: #1a4527;
  --red:      #f85149;
  --red-lt:   #2d0f0e;
  --red-bd:   #5c1b1a;
  --yellow:   #e3b341;
  --yel-lt:   #2d1f00;
  --yel-bd:   #5a3d00;
  --blue:     #58a6ff;
  --blue-lt:  #0d1f3c;
  --blue-bd:  #1a3a6e;
  --purple:   #bc8cff;
  --pur-lt:   #1e0f3c;
  --cyan:     #39d0f5;
  --cyn-lt:   #0a2535;
  --orange:   #f0883e;
  --orn-lt:   #2d1600;
}
:root[data-theme="light"] {
  --bg:       #f6f8fa;
  --bg2:      #ffffff;
  --bg3:      #f0f2f5;
  --bg4:      #e8ecf0;
  --border:   #d0d7de;
  --border2:  #adb5bd;
  --text:     #1f2328;
  --text2:    #4b5563;
  --text3:    #6e7781;
  --text4:    #b0bcc8;

  --green:    #1a7f37;
  --green-lt: #dafbe1;
  --green-bd: #a7f3c0;
  --red:      #cf222e;
  --red-lt:   #ffebe9;
  --red-bd:   #ffb8b0;
  --yellow:   #9a6700;
  --yel-lt:   #fff8c5;
  --yel-bd:   #f7d67e;
  --blue:     #0969da;
  --blue-lt:  #ddf4ff;
  --blue-bd:  #80ccff;
  --purple:   #6639ba;
  --pur-lt:   #fbefff;
  --cyan:     #0891b2;
  --cyn-lt:   #e0f9ff;
  --orange:   #bc4c00;
  --orn-lt:   #fff1e5;
}

* { box-sizing:border-box; margin:0; padding:0 }
html { scroll-behavior:smooth }
body {
  background:var(--bg);
  color:var(--text);
  font-family:'IBM Plex Sans',sans-serif;
  font-size:14px;
  line-height:1.6;
  min-height:100vh;
  transition:background .2s, color .2s;
}

/* ── Layout ── */
.shell { display:grid; grid-template-columns:256px 1fr; min-height:100vh }

/* ── Sidebar ── */
.sidebar {
  background:var(--bg2);
  border-right:1px solid var(--border);
  position:sticky; top:0; height:100vh;
  overflow-y:auto;
  display:flex; flex-direction:column;
  padding-bottom:16px;
}
.brand {
  padding:18px 16px 14px;
  border-bottom:1px solid var(--border);
}
.brand-row {
  display:flex; align-items:center; gap:10px; margin-bottom:8px;
}
.brand-icon {
  width:34px; height:34px;
  background:linear-gradient(135deg,#e83a3a,#ff6b35);
  border-radius:8px;
  display:flex; align-items:center; justify-content:center;
  font-size:17px;
  box-shadow:0 3px 10px rgba(232,58,58,.4);
  flex-shrink:0;
}
.brand-title { font-size:14px; font-weight:700; letter-spacing:-.02em }
.brand-sub { font-size:11px; color:var(--text3); margin-bottom:10px }
.backend-pill {
  display:inline-flex; align-items:center; gap:6px;
  background:var(--bg3); border:1px solid var(--border);
  border-radius:20px; padding:3px 9px;
  font-size:11px; font-family:'IBM Plex Mono',monospace;
  color:var(--blue); font-weight:500;
}
.bp-dot {
  width:5px; height:5px; border-radius:50%;
  background:var(--green);
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.8)} }

.theme-toggle {
  background:none; border:1px solid var(--border);
  border-radius:6px; color:var(--text2);
  cursor:pointer; padding:4px 9px; font-size:12px;
  margin-top:8px; display:inline-flex; align-items:center; gap:5px;
  font-family:'IBM Plex Sans',sans-serif;
  transition:all .15s;
}
.theme-toggle:hover { background:var(--bg3); color:var(--text) }

.nav-section {
  padding:14px 14px 3px;
  font-size:9px; font-weight:700;
  letter-spacing:.12em; text-transform:uppercase;
  color:var(--text4);
}
.nav-btn {
  display:flex; align-items:center; gap:9px;
  width:calc(100% - 12px); margin:1px 6px;
  padding:7px 10px;
  background:none; border:none;
  color:var(--text2); cursor:pointer;
  font-size:12.5px; font-family:'IBM Plex Sans',sans-serif;
  font-weight:500; text-align:left;
  border-radius:6px; transition:all .13s;
}
.nav-btn:hover { background:var(--bg3); color:var(--text) }
.nav-btn.active { background:var(--blue-lt); color:var(--blue); font-weight:600 }
.nav-ic { width:16px; text-align:center; flex-shrink:0; font-size:13px }
.nav-tag {
  margin-left:auto; font-size:9px; padding:1px 5px; border-radius:3px;
  background:var(--bg4); color:var(--text3); font-weight:600;
}

/* Temp widget in sidebar */
.temp-widget {
  margin:10px 12px;
  background:var(--bg3); border:1px solid var(--border);
  border-radius:8px; padding:10px 12px;
}
.tw-header { font-size:9px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:var(--text3); margin-bottom:6px }
.tw-row { display:flex; align-items:baseline; gap:8px }
.tw-temp { font-size:22px; font-weight:700; font-family:'IBM Plex Mono',monospace; transition:color .4s }
.tw-unit { font-size:12px; color:var(--text3) }
.tw-meta { font-size:10px; color:var(--text3); margin-top:3px }
.tw-bar { margin-top:6px; height:3px; background:var(--bg4); border-radius:2px; overflow:hidden }
.tw-bar-fill { height:100%; border-radius:2px; transition:width .4s, background .4s }
.tw-throttle { font-size:10px; color:var(--red); margin-top:3px; display:none }

.sb-footer {
  margin-top:auto; padding:12px 14px;
  border-top:1px solid var(--border);
  font-size:11px; color:var(--text3);
  display:flex; align-items:center; gap:6px;
}

/* ── Main ── */
.main { padding:24px 28px; overflow-x:hidden; min-width:0 }
.page { display:none }
.page.active { display:block; animation:fadeIn .18s ease }
@keyframes fadeIn { from{opacity:0;transform:translateY(3px)} to{opacity:1;transform:none} }
.page-header { margin-bottom:20px }
.page-title { font-size:20px; font-weight:700; letter-spacing:-.03em; margin-bottom:2px }
.page-sub { color:var(--text3); font-size:12px }

/* ── Cards ── */
.card {
  background:var(--bg2); border:1px solid var(--border);
  border-radius:10px; padding:16px 18px; margin-bottom:14px;
}
.card-title {
  font-size:9px; font-weight:700; letter-spacing:.1em;
  text-transform:uppercase; color:var(--text3); margin-bottom:10px;
}

/* ── Test Grid ── */
.test-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(160px,1fr));
  gap:10px; margin-bottom:18px;
}
.test-card {
  background:var(--bg2); border:1px solid var(--border);
  border-radius:10px; padding:14px; cursor:pointer;
  transition:all .16s; position:relative; overflow:hidden;
  border-left:3px solid var(--border);
}
.test-card:hover { border-color:var(--blue); background:var(--bg3); transform:translateY(-1px) }
.test-card.running { border-left-color:var(--yellow); background:var(--yel-lt) }
.test-card.pass    { border-left-color:var(--green);  background:var(--green-lt) }
.test-card.fail    { border-left-color:var(--red);    background:var(--red-lt) }
.tc-icon { font-size:22px; margin-bottom:7px }
.tc-name { font-size:12px; font-weight:600; margin-bottom:2px }
.tc-desc { font-size:10px; color:var(--text3) }
.tc-badge {
  position:absolute; top:8px; right:8px;
  font-size:9px; font-weight:700; padding:2px 7px; border-radius:3px; letter-spacing:.05em;
}
.tc-badge.pass { background:var(--green-lt); color:var(--green) }
.tc-badge.fail { background:var(--red-lt); color:var(--red) }
.tc-badge.running { background:var(--yel-lt); color:var(--yellow); animation:pulse 1s infinite }

/* ── Terminal ── */
.term-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:5px }
.term-label { display:flex; align-items:center; gap:6px; font-size:10px; color:var(--text3); font-family:'IBM Plex Mono',monospace }
.term-dot { width:6px; height:6px; border-radius:50%; background:var(--green); animation:pulse 2s infinite }
.term {
  background:var(--bg);
  border:1px solid var(--border);
  border-radius:8px;
  font-family:'IBM Plex Mono',monospace;
  font-size:11.5px; line-height:1.75;
  padding:12px 14px;
  min-height:140px; max-height:400px;
  overflow-y:auto; white-space:pre-wrap; word-break:break-all;
}
.term .pass   { color:var(--green) }
.term .fail   { color:var(--red) }
.term .warn   { color:var(--yellow) }
.term .info   { color:var(--blue) }
.term .header { color:var(--cyan); font-weight:600 }
.term .normal { color:var(--text2) }

/* ── Buttons ── */
.btn {
  display:inline-flex; align-items:center; gap:6px;
  padding:7px 14px; border-radius:6px;
  border:1px solid var(--border); background:var(--bg2);
  color:var(--text); cursor:pointer;
  font-size:12px; font-family:'IBM Plex Sans',sans-serif;
  font-weight:500; transition:all .13s;
}
.btn:hover { background:var(--bg3); border-color:var(--border2) }
.btn:disabled { opacity:.35; cursor:not-allowed }
.btn-primary {
  background:var(--blue); border-color:var(--blue); color:#fff;
  box-shadow:0 0 0 0 rgba(88,166,255,0);
  transition:all .13s, box-shadow .2s;
}
.btn-primary:hover { background:#79b8ff; box-shadow:0 0 12px rgba(88,166,255,.4) }
.btn-success { background:var(--green); border-color:var(--green); color:#fff }
.btn-success:hover { filter:brightness(1.1) }
.btn-danger  { background:var(--red); border-color:var(--red); color:#fff }
.btn-run-all {
  width:100%; justify-content:center;
  padding:11px; font-size:14px; font-weight:600;
  background:linear-gradient(90deg,#58a6ff,#bc8cff);
  border:none; color:#fff; border-radius:8px;
  box-shadow:0 4px 18px rgba(88,166,255,.25);
  margin-bottom:16px; letter-spacing:-.01em;
  transition:all .2s;
}
.btn-run-all:hover { filter:brightness(1.12); transform:translateY(-1px); box-shadow:0 6px 22px rgba(88,166,255,.35) }
.clr-btn {
  background:none; border:1px solid var(--border); border-radius:5px;
  color:var(--text3); cursor:pointer; padding:2px 9px; font-size:10px;
  font-family:'IBM Plex Sans',sans-serif; transition:all .12s;
}
.clr-btn:hover { color:var(--text); border-color:var(--border2) }

/* ── Wiring ── */
.wiring {
  background:var(--bg3); border:1px solid var(--border);
  border-left:3px solid var(--blue); border-radius:0 6px 6px 0;
  padding:10px 14px; font-family:'IBM Plex Mono',monospace;
  font-size:11.5px; color:var(--text2); line-height:2; margin-bottom:12px;
}
.wiring-title { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:var(--blue); margin-bottom:5px }

/* ── Stat grid ── */
.stat-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); gap:10px; margin-bottom:16px }
.stat-card { background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:12px 14px }
.stat-label { font-size:9px; color:var(--text3); font-weight:700; text-transform:uppercase; letter-spacing:.07em; margin-bottom:4px }
.stat-val { font-size:18px; font-weight:700; font-family:'IBM Plex Mono',monospace }
.sv-blue   { color:var(--blue) }
.sv-green  { color:var(--green) }
.sv-red    { color:var(--red) }
.sv-yellow { color:var(--yellow) }
.sv-cyan   { color:var(--cyan) }

/* ── 40-Pin Map ── */
.pin-map-wrap { overflow-x:auto }
.pin-map-table {
  border-collapse:separate; border-spacing:3px 4px;
  font-size:11px; font-family:'IBM Plex Mono',monospace;
  margin:0 auto;
}
.pin-map-table td { vertical-align:middle; padding:0 }
.pin-cell-left { text-align:right }
.pin-cell-right { text-align:left }
.pin-label {
  display:inline-flex; align-items:center;
  padding:3px 9px; border-radius:5px;
  font-weight:500; font-size:10.5px; white-space:nowrap;
  cursor:pointer; transition:all .12s;
}
.pin-label:hover { filter:brightness(1.15); transform:scale(1.03) }
.pin-num {
  width:24px; height:24px; border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:9px; font-weight:700; cursor:pointer;
  transition:transform .12s;
  border:2px solid rgba(255,255,255,.2); color:#fff; flex-shrink:0;
}
.pin-num:hover { transform:scale(1.2) }
.pin-sep { width:18px; text-align:center; color:var(--border2); font-size:14px }
.pin-desc { color:var(--text3); font-size:10px; max-width:120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:0 6px }

/* pin-type colors */
.pt-PWR33 { background:#2d0f0e; color:#fca5a5; border:1px solid #7f1d1d }
.pn-PWR33 { background:#dc2626 }
.pt-PWR5  { background:#2d1600; color:#fdba74; border:1px solid #7c2d12 }
.pn-PWR5  { background:#ea580c }
.pt-GND   { background:var(--bg4); color:var(--text2); border:1px solid var(--border) }
.pn-GND   { background:#4b5563 }
.pt-GPIO  { background:var(--blue-lt); color:var(--blue); border:1px solid var(--blue-bd) }
.pn-GPIO  { background:#2563eb }
.pt-I2C   { background:var(--green-lt); color:var(--green); border:1px solid var(--green-bd) }
.pn-I2C   { background:#16a34a }
.pt-SPI   { background:var(--orn-lt); color:var(--orange); border:1px solid #7c2d12 }
.pn-SPI   { background:#c2410c }
.pt-UART  { background:var(--yel-lt); color:var(--yellow); border:1px solid var(--yel-bd) }
.pn-UART  { background:#d97706 }
.pt-PWM   { background:var(--pur-lt); color:var(--purple); border:1px solid #4c1d95 }
.pn-PWM   { background:#7c3aed }
.pt-ID    { background:var(--bg3); color:var(--text3); border:1px solid var(--border) }
.pn-ID    { background:#64748b }
.pt-ONEWIRE { background:var(--cyn-lt); color:var(--cyan); border:1px solid #164e63 }
.pn-ONEWIRE { background:#0891b2 }

/* light-mode overrides for pin colors */
[data-theme="light"] .pt-PWR33 { background:#fef2f2; color:#991b1b; border-color:#fecaca }
[data-theme="light"] .pt-PWR5  { background:#fff7ed; color:#9a3412; border-color:#fed7aa }
[data-theme="light"] .pt-GND   { background:#f9fafb; color:#374151; border-color:#d1d5db }
[data-theme="light"] .pt-GPIO  { background:#eff6ff; color:#1e40af; border-color:#bfdbfe }
[data-theme="light"] .pt-I2C   { background:#f0fdf4; color:#14532d; border-color:#bbf7d0 }
[data-theme="light"] .pt-SPI   { background:#fff7ed; color:#7c2d12; border-color:#fed7aa }
[data-theme="light"] .pt-UART  { background:#fefce8; color:#713f12; border-color:#fde68a }
[data-theme="light"] .pt-PWM   { background:#faf5ff; color:#581c87; border-color:#e9d5ff }
[data-theme="light"] .pt-ID    { background:#f8fafc; color:#475569; border-color:#cbd5e1 }
[data-theme="light"] .pt-ONEWIRE { background:#ecfeff; color:#164e63; border-color:#a5f3fc }

/* Pin legend */
.pin-legend { display:flex; flex-wrap:wrap; gap:5px; margin-bottom:14px }
.legend-item { display:inline-flex; align-items:center; gap:4px; padding:2px 9px; border-radius:12px; font-size:10px; font-weight:500; cursor:pointer; transition:opacity .13s }
.legend-item:hover { opacity:.75 }

/* ── Pin detail panel ── */
.pin-detail-panel {
  background:var(--bg3); border:1px solid var(--border);
  border-radius:8px; padding:14px 16px; margin-top:12px; display:none;
}
.pin-detail-panel.visible { display:block; animation:fadeIn .18s ease }
.pd-header { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap }
.pd-badge { padding:4px 10px; border-radius:5px; font-size:12px; font-weight:600; font-family:'IBM Plex Mono',monospace }
.pd-code {
  background:var(--bg); border:1px solid var(--border); border-radius:6px;
  padding:8px 12px; font-family:'IBM Plex Mono',monospace;
  font-size:11.5px; color:var(--blue); margin-top:8px; line-height:1.9;
}
.pd-alts { margin-top:10px }
.pd-alts-title { font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:var(--text3); margin-bottom:6px }
.alt-table { width:100%; border-collapse:collapse; font-size:11px; font-family:'IBM Plex Mono',monospace }
.alt-table td { padding:3px 8px; border-bottom:1px solid var(--border) }
.alt-table td:first-child { color:var(--text3); width:60px }
.alt-table tr:last-child td { border-bottom:none }
.alt-fn-uart { color:var(--yellow) }
.alt-fn-spi  { color:var(--orange) }
.alt-fn-i2c  { color:var(--green) }
.alt-fn-pwm  { color:var(--purple) }
.alt-fn-pcm  { color:var(--cyan) }
.alt-fn-jtag { color:var(--red) }
.alt-fn-clk  { color:var(--text2) }

/* GPIO toggle buttons on pin map */
.pin-toggle-btn {
  display:inline-flex; align-items:center; gap:3px;
  padding:2px 7px; border-radius:4px; font-size:9px;
  font-weight:600; border:1px solid var(--border);
  cursor:pointer; background:var(--bg4); color:var(--text2);
  font-family:'IBM Plex Mono',monospace; transition:all .12s; margin-left:4px;
}
.pin-toggle-btn:hover { background:var(--blue); border-color:var(--blue); color:#fff }
.pin-toggle-btn.high { background:var(--green); border-color:var(--green); color:#fff }
.pin-toggle-btn.low  { background:var(--bg4); color:var(--text3) }

/* ── Alt Functions Table ── */
.alt-full-table {
  width:100%; border-collapse:collapse;
  font-size:11.5px; font-family:'IBM Plex Mono',monospace;
}
.alt-full-table th {
  padding:8px 10px; text-align:left;
  background:var(--bg3); border-bottom:2px solid var(--border);
  font-size:9px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--text3); font-weight:700; white-space:nowrap;
}
.alt-full-table td {
  padding:5px 10px; border-bottom:1px solid var(--border);
  vertical-align:middle; white-space:nowrap;
}
.alt-full-table tr:hover td { background:var(--bg3) }
.alt-full-table .col-pin { color:var(--text2); font-weight:600 }
.alt-full-table .col-label { font-weight:600 }
.alt-chip { display:inline-flex; align-items:center; padding:2px 8px; border-radius:4px; font-size:10px; font-weight:500 }
.ach-uart { background:var(--yel-lt); color:var(--yellow) }
.ach-spi  { background:var(--orn-lt); color:var(--orange) }
.ach-i2c  { background:var(--green-lt); color:var(--green) }
.ach-pwm  { background:var(--pur-lt); color:var(--purple) }
.ach-pcm  { background:var(--cyn-lt); color:var(--cyan) }
.ach-jtag { background:var(--red-lt); color:var(--red) }
.ach-clk  { background:var(--bg4); color:var(--text2) }
.ach-dpi  { background:var(--bg3); color:var(--text3) }
.ach-none { color:var(--text4) }

/* Pull-state badges in alt-fn table */
.pull-badge { display:inline-flex; align-items:center; padding:1px 7px; border-radius:3px; font-size:9px; font-weight:700; font-family:'IBM Plex Mono',monospace }
.hpull-hi { background:var(--green-lt); color:var(--green); border:1px solid var(--green-bd) }
.hpull-lo { background:var(--bg4); color:var(--text3); border:1px solid var(--border) }

/* ── History ── */
.history-row {
  display:flex; align-items:center; gap:10px;
  padding:8px 12px; border-bottom:1px solid var(--border);
  font-size:12px;
}
.history-row:last-child { border-bottom:none }
.history-name { font-weight:600; flex:1 }
.history-time { font-family:'IBM Plex Mono',monospace; font-size:10px; color:var(--text3); flex-shrink:0 }
.history-badge { padding:2px 8px; border-radius:3px; font-size:10px; font-weight:700 }
.hb-pass { background:var(--green-lt); color:var(--green) }
.hb-fail { background:var(--red-lt); color:var(--red) }
.hb-mixed { background:var(--yel-lt); color:var(--yellow) }

/* ── Manual GPIO ── */
.gpio-ctrl-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:14px }
.form-group { display:flex; flex-direction:column; gap:4px }
.form-group label { font-size:10px; color:var(--text2); font-weight:600; text-transform:uppercase; letter-spacing:.05em }
.form-input {
  background:var(--bg3); border:1px solid var(--border); border-radius:6px;
  color:var(--text); padding:7px 10px; font-size:12.5px;
  font-family:'IBM Plex Sans',sans-serif; width:100%; transition:border-color .12s;
}
.form-input:focus { outline:none; border-color:var(--blue); background:var(--bg2) }
.gpio-result {
  margin-top:8px; padding:8px 12px; border-radius:6px;
  font-family:'IBM Plex Mono',monospace; font-size:12px; font-weight:500; display:none;
}
.gpio-result.pass { background:var(--green-lt); border:1px solid var(--green-bd); color:var(--green) }
.gpio-result.fail { background:var(--red-lt); border:1px solid var(--red-bd); color:var(--red) }

/* ── Config ── */
.cfg-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px }
.cfg-status { font-size:11px; color:var(--text3); margin-left:10px }
.cfg-status.ok { color:var(--green) }

/* ── Log ── */
.log-line { padding:2px 0; border-bottom:1px solid var(--bg3); font-size:11px; font-family:'IBM Plex Mono',monospace; color:var(--text2) }
.log-line.pass { color:var(--green) }
.log-line.fail { color:var(--red) }
.log-line.warn { color:var(--yellow) }

/* ── Toast ── */
.toast {
  position:fixed; bottom:20px; right:20px;
  background:var(--bg2); border:1px solid var(--border);
  border-radius:8px; padding:10px 14px; font-size:12px; font-weight:500;
  box-shadow:0 8px 24px rgba(0,0,0,.4); z-index:9999;
  transition:all .25s; display:flex; align-items:center; gap:7px;
}
.toast.hidden { opacity:0; transform:translateY(6px); pointer-events:none }
.toast.pass { border-color:var(--green-bd); background:var(--green-lt); color:var(--green) }
.toast.fail { border-color:var(--red-bd); background:var(--red-lt); color:var(--red) }
.toast.info { border-color:var(--blue-bd); background:var(--blue-lt); color:var(--blue) }

/* ── Info badge ── */
.info-badge {
  display:inline-flex; align-items:center; gap:4px;
  padding:4px 10px; border-radius:6px; font-size:11px; margin-bottom:12px;
}
.ib-blue  { background:var(--blue-lt); border:1px solid var(--blue-bd); color:var(--blue) }
.ib-yel   { background:var(--yel-lt); border:1px solid var(--yel-bd); color:var(--yellow) }
.ib-red   { background:var(--red-lt); border:1px solid var(--red-bd); color:var(--red) }

/* ── Tab bar ── */
.tab-bar { display:flex; gap:2px; margin-bottom:16px; border-bottom:1px solid var(--border); padding-bottom:0 }
.tab-btn {
  padding:7px 14px; background:none; border:none; cursor:pointer;
  font-size:12px; font-weight:500; color:var(--text3);
  font-family:'IBM Plex Sans',sans-serif; border-bottom:2px solid transparent;
  margin-bottom:-1px; transition:all .14s;
}
.tab-btn:hover { color:var(--text) }
.tab-btn.active { color:var(--blue); border-bottom-color:var(--blue) }
.tab-pane { display:none }
.tab-pane.active { display:block; animation:fadeIn .15s }

/* ── Divider ── */
.divider { height:1px; background:var(--border); margin:14px 0 }

/* ── Responsive ── */
@media(max-width:700px) {
  .shell { grid-template-columns:1fr }
  .sidebar { position:static; height:auto }
  .main { padding:14px }
  .gpio-ctrl-grid, .cfg-grid { grid-template-columns:1fr }
}

::-webkit-scrollbar { width:4px; height:4px }
::-webkit-scrollbar-track { background:transparent }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:2px }
</style>
</head>
<body>
<div class="shell">

<!-- ═══════════════ SIDEBAR ═══════════════ -->
<nav class="sidebar">
  <div class="brand">
    <div class="brand-row">
      <div class="brand-icon">🍓</div>
      <div>
        <div class="brand-title">RPi Test Suite</div>
      </div>
    </div>
    <div class="brand-sub">Hardware diagnostic dashboard v2.1</div>
    <div class="backend-pill">
      <span class="bp-dot"></span>
      <span id="backend-name">loading…</span>
    </div>
    <br>
    <button class="theme-toggle" onclick="toggleTheme()">
      <span id="theme-icon">☀️</span> <span id="theme-label">Light mode</span>
    </button>
  </div>

  <!-- Live temp widget -->
  <div class="temp-widget">
    <div class="tw-header">CPU Temperature</div>
    <div class="tw-row">
      <div class="tw-temp" id="tw-temp">—</div>
      <div class="tw-unit">°C</div>
    </div>
    <div class="tw-bar"><div class="tw-bar-fill" id="tw-bar" style="width:0%"></div></div>
    <div class="tw-meta" id="tw-meta">ARM: — MHz</div>
    <div class="tw-throttle" id="tw-throttle">⚡ Throttling detected</div>
  </div>

  <div class="nav-section">Tests</div>
  <button class="nav-btn active" onclick="sp('dashboard')" id="nav-dashboard"><span class="nav-ic">⊞</span>Dashboard</button>
  <button class="nav-btn" onclick="sp('gpio')"      id="nav-gpio"><span class="nav-ic">⚡</span>GPIO<span class="nav-tag">3 modes</span></button>
  <button class="nav-btn" onclick="sp('pwm')"       id="nav-pwm"><span class="nav-ic">〜</span>PWM<span class="nav-tag">LED+Servo</span></button>
  <button class="nav-btn" onclick="sp('i2c')"       id="nav-i2c"><span class="nav-ic">🔵</span>I2C</button>
  <button class="nav-btn" onclick="sp('spi')"       id="nav-spi"><span class="nav-ic">🟠</span>SPI</button>
  <button class="nav-btn" onclick="sp('uart')"      id="nav-uart"><span class="nav-ic">🔴</span>UART</button>
  <button class="nav-btn" onclick="sp('onewire')"   id="nav-onewire"><span class="nav-ic">🌡️</span>1-Wire DS18B20</button>

  <div class="nav-section">Tools</div>
  <button class="nav-btn" onclick="sp('manual')"    id="nav-manual"><span class="nav-ic">🎛️</span>Manual GPIO</button>
  <button class="nav-btn" onclick="sp('pinmap')"    id="nav-pinmap"><span class="nav-ic">📍</span>40-Pin Map</button>
  <button class="nav-btn" onclick="sp('altfn')"     id="nav-altfn"><span class="nav-ic">🔀</span>Alt Functions</button>
  <button class="nav-btn" onclick="sp('sysinfo')"   id="nav-sysinfo"><span class="nav-ic">📊</span>System Info</button>
  <button class="nav-btn" onclick="sp('history')"   id="nav-history"><span class="nav-ic">🗂️</span>Test History</button>
  <button class="nav-btn" onclick="sp('logview')"   id="nav-logview"><span class="nav-ic">📋</span>Log Viewer</button>
  <button class="nav-btn" onclick="sp('config')"    id="nav-config"><span class="nav-ic">⚙️</span>Config</button>
  <button class="nav-btn" onclick="sp('backends')"  id="nav-backends"><span class="nav-ic">🔧</span>Backends</button>

  <div class="sb-footer">
    <span id="sb-dot" style="width:6px;height:6px;border-radius:50%;background:var(--green);flex-shrink:0"></span>
    <span id="sb-status">Ready</span>
  </div>
</nav>

<!-- ═══════════════ MAIN ═══════════════ -->
<main class="main">

<!-- ── Dashboard ── -->
<div class="page active" id="page-dashboard">
  <div class="page-header">
    <div class="page-title">Dashboard</div>
    <div class="page-sub">Click any card to run a test · Live CPU temp in sidebar</div>
  </div>
  <button class="btn btn-run-all" onclick="run('all','td-out')">▶  Run All Tests</button>
  <div class="test-grid">
    <div class="test-card" id="tc-gpio"      onclick="run('gpio','td-out')"><div class="tc-icon">⚡</div><div class="tc-name">GPIO Loopback</div><div class="tc-desc">3 pairs · HIGH+LOW</div></div>
    <div class="test-card" id="tc-gpio_pull" onclick="run('gpio_pull','td-out')"><div class="tc-icon">🔌</div><div class="tc-name">Pull R/R</div><div class="tc-desc">No wiring needed</div></div>
    <div class="test-card" id="tc-gpio_irq"  onclick="run('gpio_irq','td-out')"><div class="tc-icon">🔔</div><div class="tc-name">GPIO Interrupt</div><div class="tc-desc">Rising edge detect</div></div>
    <div class="test-card" id="tc-pwm"       onclick="run('pwm','td-out')"><div class="tc-icon">💡</div><div class="tc-name">PWM LED Ramp</div><div class="tc-desc">0→100% duty cycle</div></div>
    <div class="test-card" id="tc-servo"     onclick="run('servo','td-out')"><div class="tc-icon">⚙️</div><div class="tc-name">Servo Sweep</div><div class="tc-desc">0°→180°→0°</div></div>
    <div class="test-card" id="tc-i2c"       onclick="run('i2c','td-out')"><div class="tc-icon">🔵</div><div class="tc-name">I2C Scan</div><div class="tc-desc">Detect bus devices</div></div>
    <div class="test-card" id="tc-spi"       onclick="run('spi','td-out')"><div class="tc-icon">🟠</div><div class="tc-name">SPI Loopback</div><div class="tc-desc">MOSI↔MISO bridge</div></div>
    <div class="test-card" id="tc-uart"      onclick="run('uart','td-out')"><div class="tc-icon">🔴</div><div class="tc-name">UART Loopback</div><div class="tc-desc">TX↔RX bridge</div></div>
    <div class="test-card" id="tc-onewire"   onclick="run('onewire','td-out')"><div class="tc-icon">🌡️</div><div class="tc-name">1-Wire DS18B20</div><div class="tc-desc">Temperature sensor</div></div>
    <div class="test-card" id="tc-deps"      onclick="run('deps','td-out')"><div class="tc-icon">📦</div><div class="tc-name">Dependencies</div><div class="tc-desc">Check installed libs</div></div>
  </div>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('td-out')">Clear</button>
  </div>
  <div class="term" id="td-out"><span style="color:var(--text4)">Click a card above to start a test…</span></div>
</div>

<!-- ── GPIO ── -->
<div class="page" id="page-gpio">
  <div class="page-header"><div class="page-title">⚡ GPIO Tests</div><div class="page-sub">Loopback, interrupt edge detection, pull-up/pull-down</div></div>
  <div class="tab-bar">
    <button class="tab-btn active" onclick="showTab('gpio','loopback')">Loopback</button>
    <button class="tab-btn" onclick="showTab('gpio','interrupt')">Interrupt</button>
    <button class="tab-btn" onclick="showTab('gpio','pull')">Pull R/R</button>
  </div>
  <div class="tab-pane active" id="gpio-tab-loopback">
    <div class="wiring"><div class="wiring-title">Required Wiring</div>GPIO17 (Pin 11) ↔ GPIO27 (Pin 13)<br>GPIO22 (Pin 15) ↔ GPIO23 (Pin 16)<br>GPIO24 (Pin 18) ↔ GPIO25 (Pin 22)</div>
    <button class="btn btn-primary" style="margin-bottom:12px" onclick="run('gpio','tg-out')">▶ Run Loopback Test</button>
  </div>
  <div class="tab-pane" id="gpio-tab-interrupt">
    <div class="wiring"><div class="wiring-title">Required Wiring</div>GPIO17 (Pin 11) → GPIO27 (Pin 13) — one jumper wire</div>
    <button class="btn btn-primary" style="margin-bottom:12px" onclick="run('gpio_irq','tg-out')">▶ Run Interrupt Test</button>
  </div>
  <div class="tab-pane" id="gpio-tab-pull">
    <div class="info-badge ib-blue">💡 No external wiring required — tests internal pull-up and pull-down resistors</div><br>
    <button class="btn btn-primary" style="margin-bottom:12px" onclick="run('gpio_pull','tg-out')">▶ Run Pull R/R Test</button>
  </div>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('tg-out')">Clear</button></div>
  <div class="term" id="tg-out"></div>
</div>

<!-- ── PWM ── -->
<div class="page" id="page-pwm">
  <div class="page-header"><div class="page-title">〜 PWM Tests</div><div class="page-sub">LED brightness ramp and servo sweep</div></div>
  <div class="wiring"><div class="wiring-title">LED Ramp</div>GPIO18 (Pin 12) → 330Ω → LED(+) → LED(−) → GND (Pin 6)</div>
  <div class="wiring"><div class="wiring-title">Servo</div>Signal → GPIO18 (Pin 12) · VCC → 5V (Pin 2) · GND → Pin 6</div>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn btn-primary" onclick="run('pwm','tp-out')">▶ LED Ramp</button>
    <button class="btn" onclick="run('servo','tp-out')">▶ Servo Sweep</button>
  </div>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('tp-out')">Clear</button></div>
  <div class="term" id="tp-out"></div>
</div>

<!-- ── I2C ── -->
<div class="page" id="page-i2c">
  <div class="page-header"><div class="page-title">🔵 I2C Scan</div><div class="page-sub">Detect and read devices on I2C bus</div></div>
  <div class="wiring"><div class="wiring-title">Wiring</div>SDA → Pin 3 · SCL → Pin 5 · 3.3V → Pin 1</div>
  <div class="info-badge ib-blue">💡 Enable I2C: <code>sudo raspi-config → Interface Options → I2C → Yes</code></div>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn btn-primary" onclick="run('i2c','ti-out')">▶ Scan Bus</button>
  </div>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('ti-out')">Clear</button></div>
  <div class="term" id="ti-out"></div>
</div>

<!-- ── SPI ── -->
<div class="page" id="page-spi">
  <div class="page-header"><div class="page-title">🟠 SPI Loopback</div><div class="page-sub">Echoes 0xDEADBEEF via MOSI↔MISO bridge</div></div>
  <div class="wiring"><div class="wiring-title">Wiring</div>MOSI (Pin 19) ↔ MISO (Pin 21) — one jumper wire</div>
  <div class="info-badge ib-yel">⚠ Enable SPI: <code>sudo raspi-config → Interface Options → SPI → Yes</code></div>
  <button class="btn btn-primary" onclick="run('spi','ts-out')" style="margin-bottom:12px">▶ SPI Loopback</button>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('ts-out')">Clear</button></div>
  <div class="term" id="ts-out"></div>
</div>

<!-- ── UART ── -->
<div class="page" id="page-uart">
  <div class="page-header"><div class="page-title">🔴 UART Loopback</div><div class="page-sub">Sends a test string and reads it back</div></div>
  <div class="wiring"><div class="wiring-title">Wiring</div>TX (Pin 8) ↔ RX (Pin 10) — one jumper wire</div>
  <div class="info-badge ib-red">⚠ Disable serial console first: raspi-config → Interface → Serial Port → login shell: No → port: Yes</div>
  <button class="btn btn-primary" onclick="run('uart','tu-out')" style="margin-bottom:12px">▶ UART Loopback</button>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('tu-out')">Clear</button></div>
  <div class="term" id="tu-out"></div>
</div>

<!-- ── 1-Wire ── -->
<div class="page" id="page-onewire">
  <div class="page-header"><div class="page-title">🌡️ 1-Wire / DS18B20</div><div class="page-sub">Scan and read DS18B20 temperature sensors</div></div>
  <div class="wiring"><div class="wiring-title">DS18B20 Wiring</div>VCC → 3.3V (Pin 1) · GND → Pin 6 · DATA → GPIO4 (Pin 7)<br>4.7kΩ pull-up between DATA and VCC</div>
  <div class="info-badge ib-blue">💡 Enable 1-Wire: <code>sudo raspi-config → Interface Options → 1-Wire → Yes</code> then reboot</div>
  <button class="btn btn-primary" onclick="run('onewire','tow-out')" style="margin-bottom:12px">▶ Scan &amp; Read Sensors</button>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Output</div><button class="clr-btn" onclick="clr('tow-out')">Clear</button></div>
  <div class="term" id="tow-out"></div>
</div>

<!-- ── Manual GPIO ── -->
<div class="page" id="page-manual">
  <div class="page-header"><div class="page-title">🎛️ Manual GPIO Control</div><div class="page-sub">Set output, read input, or live snapshot any BCM pin</div></div>
  <div class="gpio-ctrl-grid">
    <div class="card">
      <div class="card-title">Set Output Pin</div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <div class="form-group" style="flex:1"><label>BCM Pin</label><input type="number" class="form-input" id="out-pin" min="2" max="27" value="18"></div>
        <div class="form-group" style="flex:1"><label>Value</label><select class="form-input" id="out-val"><option value="1">HIGH (1)</option><option value="0">LOW (0)</option></select></div>
      </div>
      <button class="btn btn-success" onclick="gpioSet()">▶ Set Pin</button>
      <div class="gpio-result" id="out-result"></div>
    </div>
    <div class="card">
      <div class="card-title">Read Input Pin</div>
      <div style="margin-bottom:10px">
        <div class="form-group"><label>BCM Pin</label><input type="number" class="form-input" id="in-pin" min="2" max="27" value="17"></div>
      </div>
      <button class="btn btn-primary" onclick="gpioRead()">▶ Read Pin</button>
      <div class="gpio-result" id="in-result"></div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Live Pin Snapshot</div>
    <div style="font-size:11px;color:var(--text3);margin-bottom:8px">Reads from kernel sysfs — no pins reconfigured</div>
    <button class="btn btn-primary" onclick="run('snapshot','tsnap-out')" style="margin-bottom:10px">↻ Refresh Snapshot</button>
    <div class="term-header"><div class="term-label"><span class="term-dot"></span>Snapshot</div><button class="clr-btn" onclick="clr('tsnap-out')">Clear</button></div>
    <div class="term" id="tsnap-out" style="min-height:100px"></div>
  </div>
</div>

<!-- ── 40-Pin Map ── -->
<div class="page" id="page-pinmap">
  <div class="page-header"><div class="page-title">📍 40-Pin GPIO Map</div><div class="page-sub">Click any pin for details + alt functions · GPIO pins show HIGH/LOW toggle</div></div>
  <div class="card">
    <div class="pin-legend" id="pin-legend"></div>
    <div class="divider"></div>
    <div class="pin-map-wrap"><table class="pin-map-table" id="pin-map-table"></table></div>
  </div>
  <div class="pin-detail-panel" id="pin-detail-panel"><div id="pin-detail-content"></div></div>
</div>

<!-- ── Alt Functions ── -->
<div class="page" id="page-altfn">
  <div class="page-header"><div class="page-title">🔀 Alternate Functions</div><div class="page-sub">Table 5 — RPi 4 Datasheet RP-008341-DS Rev 1.1 · BCM2711 GPIO0–GPIO27 · All 28 user GPIOs · ALT0–ALT5</div></div>
  <div class="card" style="padding:0;overflow:hidden">
    <div style="overflow-x:auto"><table class="alt-full-table" id="alt-fn-table"></table></div>
  </div>
</div>

<!-- ── System Info ── -->
<div class="page" id="page-sysinfo">
  <div class="page-header"><div class="page-title">📊 System Info</div><div class="page-sub">Raspberry Pi hardware diagnostics + live temperature</div></div>
  <div class="stat-grid">
    <div class="stat-card"><div class="stat-label">GPIO Backend</div><div class="stat-val sv-blue" id="ss-backend">—</div></div>
    <div class="stat-card"><div class="stat-label">Status</div><div class="stat-val sv-green" id="ss-status">Ready</div></div>
    <div class="stat-card"><div class="stat-label">GPIO</div><div class="stat-val sv-green" id="ss-gpio">—</div></div>
    <div class="stat-card"><div class="stat-label">CPU Temp</div><div class="stat-val sv-cyan" id="ss-temp">—</div></div>
    <div class="stat-card"><div class="stat-label">ARM Freq</div><div class="stat-val sv-blue" id="ss-freq">—</div></div>
    <div class="stat-card"><div class="stat-label">Throttle</div><div class="stat-val sv-green" id="ss-throttle">OK</div></div>
  </div>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn btn-primary" onclick="run('sysinfo','tsi-out')">↻ Refresh Full Info</button>
    <button class="btn" onclick="run('tempmon','tsi-out')">🌡️ Temp Monitor</button>
  </div>
  <div class="term-header"><div class="term-label"><span class="term-dot"></span>Full Output</div><button class="clr-btn" onclick="clr('tsi-out')">Clear</button></div>
  <div class="term" id="tsi-out"></div>
</div>

<!-- ── Test History ── -->
<div class="page" id="page-history">
  <div class="page-header"><div class="page-title">🗂️ Test History</div><div class="page-sub">Last 30 test runs this session</div></div>
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn btn-primary" onclick="loadHistory()">↻ Refresh</button>
  </div>
  <div class="card" style="padding:0">
    <div id="history-list" style="max-height:500px;overflow-y:auto">
      <div style="padding:16px;color:var(--text3);font-size:12px">Click Refresh to load history…</div>
    </div>
  </div>
</div>

<!-- ── Log Viewer ── -->
<div class="page" id="page-logview">
  <div class="page-header"><div class="page-title">📋 Log Viewer</div><div class="page-sub">rpi_test.log — persistent test history</div></div>
  <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
    <button class="btn btn-primary" onclick="loadLog()">↻ Refresh</button>
    <select class="form-input" id="log-lines" onchange="loadLog()" style="width:110px">
      <option value="30">30 lines</option><option value="60" selected>60 lines</option>
      <option value="120">120 lines</option><option value="500">500 lines</option>
    </select>
  </div>
  <div class="card" style="padding:0">
    <div id="log-box" style="padding:12px;max-height:520px;overflow-y:auto">
      <span style="color:var(--text4)">Click Refresh to load log…</span>
    </div>
  </div>
</div>

<!-- ── Config ── -->
<div class="page" id="page-config">
  <div class="page-header"><div class="page-title">⚙️ Configuration</div><div class="page-sub">Saved to rpi_test.json · Applied on next run</div></div>
  <div class="card">
    <div class="cfg-grid" id="cfg-form"></div>
    <div class="divider"></div>
    <div style="display:flex;align-items:center;gap:8px">
      <button class="btn btn-primary" onclick="saveCfg()">💾 Save Config</button>
      <span class="cfg-status" id="cfg-status"></span>
    </div>
  </div>
</div>

<!-- ── Backends ── -->
<div class="page" id="page-backends">
  <div class="page-header"><div class="page-title">🔧 GPIO Backend Reference</div><div class="page-sub">Available GPIO libraries and their capabilities</div></div>
  <div id="backends-list"></div>
</div>

</main>
</div>

<div class="toast hidden" id="toast"></div>

<script>
// ── Theme ─────────────────────────────────────────────────────────────────────
let _dark = true
function toggleTheme() {
  _dark = !_dark
  document.documentElement.setAttribute('data-theme', _dark ? 'dark' : 'light')
  document.getElementById('theme-icon').textContent  = _dark ? '☀️' : '🌙'
  document.getElementById('theme-label').textContent = _dark ? 'Light mode' : 'Dark mode'
  localStorage.setItem('rpi-theme', _dark ? 'dark' : 'light')
}
;(function() {
  const saved = localStorage.getItem('rpi-theme')
  if (saved === 'light') { _dark=false; document.documentElement.setAttribute('data-theme','light')
    document.getElementById('theme-icon').textContent='🌙'
    document.getElementById('theme-label').textContent='Dark mode' }
})()

// ── Page routing ──────────────────────────────────────────────────────────────
function sp(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'))
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'))
  document.getElementById('page-'+name).classList.add('active')
  const nb=document.getElementById('nav-'+name); if(nb) nb.classList.add('active')
  if (name==='pinmap')   buildPinMap()
  if (name==='altfn')    buildAltTable()
  if (name==='logview')  loadLog()
  if (name==='config')   loadCfg()
  if (name==='sysinfo')  loadStats()
  if (name==='history')  loadHistory()
  if (name==='backends') buildBackends()
}

// ── Tab bar ───────────────────────────────────────────────────────────────────
function showTab(page, tab) {
  document.querySelectorAll(`#page-${page} .tab-pane`).forEach(p=>p.classList.remove('active'))
  document.querySelectorAll(`#page-${page} .tab-btn`).forEach(b=>b.classList.remove('active'))
  document.getElementById(`${page}-tab-${tab}`).classList.add('active')
  event.target.classList.add('active')
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, cls='') {
  const t=document.getElementById('toast')
  t.textContent=msg; t.className='toast '+cls
  clearTimeout(t._tid)
  t._tid=setTimeout(()=>t.classList.add('hidden'),3000)
}

// ── Terminal helpers ──────────────────────────────────────────────────────────
function clr(id) { document.getElementById(id).innerHTML='' }
function appendLine(id, text, cls) {
  const t=document.getElementById(id)
  const s=document.createElement('span')
  s.className=cls||'normal'; s.textContent=text+'\n'
  t.appendChild(s); t.scrollTop=t.scrollHeight
}

// ── SSE runner ────────────────────────────────────────────────────────────────
let _es=null
function run(name, termId) {
  if(_es){_es.close();_es=null}
  clr(termId); setStatus('Running '+name+'…',true)
  const card=document.getElementById('tc-'+name)
  if(card){
    card.className='test-card running'
    const ob=card.querySelector('.tc-badge'); if(ob) ob.remove()
    const b=document.createElement('div'); b.className='tc-badge running'; b.textContent='RUNNING'
    card.appendChild(b)
  }
  const es=new EventSource('/api/test/'+name); _es=es
  es.onmessage=e=>{
    if(e.data==='[DONE]'){es.close();_es=null;setStatus('Ready');return}
    if(e.data==='[TIMEOUT]'){appendLine(termId,'⚠ Timed out','warn');es.close();_es=null;setStatus('Ready');return}
    try{
      const d=JSON.parse(e.data); appendLine(termId,d.line,d.cls)
      if(card&&(d.cls==='pass'||d.cls==='fail')){
        const ob=card.querySelector('.tc-badge'); if(ob) ob.remove()
        const b=document.createElement('div'); b.className='tc-badge '+d.cls; b.textContent=d.cls.toUpperCase()
        card.appendChild(b); card.className='test-card '+d.cls
      }
    }catch{}
  }
  es.onerror=()=>{
    if(es.readyState===EventSource.CLOSED) return
    appendLine(termId,'✖ Connection error','fail')
    es.close();_es=null;setStatus('Ready')
  }
}

// ── Status bar ────────────────────────────────────────────────────────────────
function setStatus(msg,running=false){
  const dot=document.getElementById('sb-dot'); const txt=document.getElementById('sb-status')
  txt.textContent=msg
  dot.style.background=running?'var(--yellow)':'var(--green)'
}

// ── Live temperature widget ───────────────────────────────────────────────────
async function pollTemp(){
  try{
    const d=await fetch('/api/temp').then(r=>r.json())
    if(!d.ok) return
    const t=d.temp; const el=document.getElementById('tw-temp')
    el.textContent=t.toFixed(1)
    el.style.color=t<60?'var(--green)':t<75?'var(--yellow)':'var(--red)'
    const pct=Math.min(100,Math.max(0,(t-30)/55*100))
    const bar=document.getElementById('tw-bar')
    bar.style.width=pct+'%'
    bar.style.background=t<60?'var(--green)':t<75?'var(--yellow)':'var(--red)'
    if(d.freq_mhz) document.getElementById('tw-meta').textContent='ARM: '+d.freq_mhz+' MHz'
    const thr=document.getElementById('tw-throttle')
    thr.style.display=d.throttled?'block':'none'
    // update sysinfo stats too
    document.getElementById('ss-temp').textContent=t.toFixed(1)+'°C'
    if(d.freq_mhz) document.getElementById('ss-freq').textContent=d.freq_mhz+' MHz'
    if(d.throttled){
      document.getElementById('ss-throttle').textContent='⚡ Throttled'
      document.getElementById('ss-throttle').className='stat-val sv-red'
    }
  }catch{}
}
setInterval(pollTemp,4000); pollTemp()

// ── Poll status ───────────────────────────────────────────────────────────────
async function pollStatus(){
  try{
    const s=await fetch('/api/status').then(r=>r.json())
    document.getElementById('backend-name').textContent=s.backend
    document.getElementById('ss-backend').textContent=s.backend
    document.getElementById('ss-gpio').textContent=s.has_gpio?'Available':'Not found'
    if(!s.test_running) setStatus('Ready')
  }catch{}
}
setInterval(pollStatus,4000); pollStatus()

async function loadStats(){
  const s=await fetch('/api/status').then(r=>r.json())
  document.getElementById('ss-backend').textContent=s.backend
  document.getElementById('ss-gpio').textContent=s.has_gpio?'Available':'Not found'
  document.getElementById('ss-status').textContent=s.test_running?'Running…':'Ready'
}

// ── Manual GPIO ───────────────────────────────────────────────────────────────
async function gpioSet(){
  const pin=document.getElementById('out-pin').value
  const val=document.getElementById('out-val').value
  const res=await fetch('/api/gpio/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin,value:val})}).then(r=>r.json())
  const el=document.getElementById('out-result'); el.style.display='block'
  el.className='gpio-result '+(res.ok?'pass':'fail')
  el.textContent=res.ok?'✔ '+res.msg:'✖ '+res.msg
  toast(el.textContent,res.ok?'pass':'fail')
  // update pin-map toggle if visible
  const tb=document.getElementById('ptb-'+pin)
  if(tb){ tb.textContent=res.ok&&val=='1'?'HIGH':'LOW'; tb.className='pin-toggle-btn '+(val=='1'?'high':'low') }
}

async function gpioRead(){
  const pin=document.getElementById('in-pin').value
  const res=await fetch('/api/gpio/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})}).then(r=>r.json())
  const el=document.getElementById('in-result'); el.style.display='block'
  el.className='gpio-result '+(res.ok?'pass':'fail')
  el.textContent=res.ok?`✔ GPIO${pin} = ${res.label} (${res.value})`:'✖ '+res.msg
  toast(el.textContent,res.ok?'pass':'fail')
}

// ── Log viewer ────────────────────────────────────────────────────────────────
async function loadLog(){
  const n=document.getElementById('log-lines').value
  const data=await fetch('/api/log?lines='+n).then(r=>r.json())
  const c=document.getElementById('log-box'); c.innerHTML=''
  if(!data.lines.length){c.innerHTML='<span style="color:var(--text4)">No log entries yet</span>';return}
  data.lines.forEach(line=>{
    const d=document.createElement('div'); d.className='log-line'
    if(line.includes('PASS')) d.classList.add('pass')
    else if(line.includes('FAIL')) d.classList.add('fail')
    else if(line.includes('WARN')||line.includes('warn')) d.classList.add('warn')
    d.textContent=line; c.appendChild(d)
  })
  c.scrollTop=c.scrollHeight
}

// ── Test History ──────────────────────────────────────────────────────────────
async function loadHistory(){
  const data=await fetch('/api/history').then(r=>r.json())
  const list=document.getElementById('history-list'); list.innerHTML=''
  if(!data.history.length){
    list.innerHTML='<div style="padding:16px;color:var(--text3);font-size:12px">No test runs yet — run some tests first</div>'
    return
  }
  data.history.forEach(h=>{
    const total=h.passed+h.failed; const cls=h.failed===0?'hb-pass':h.passed===0?'hb-fail':'hb-mixed'
    const label=h.failed===0?'ALL PASS':h.passed===0?'ALL FAIL':`${h.passed}/${total}`
    const ts=new Date(h.timestamp).toLocaleTimeString()
    const row=document.createElement('div'); row.className='history-row'
    row.innerHTML=`<span class="history-name">${h.name}</span>
      <span class="history-time">${ts}</span>
      <span class="history-badge ${cls}">${label}</span>`
    list.appendChild(row)
  })
}

// ── Config ────────────────────────────────────────────────────────────────────
async function loadCfg(){
  const cfg=await fetch('/api/config').then(r=>r.json())
  const f=document.getElementById('cfg-form'); f.innerHTML=''
  for(const [k,v] of Object.entries(cfg)){
    const g=document.createElement('div'); g.className='form-group'
    g.innerHTML=`<label>${k}</label>
      <input type="text" class="form-input" id="cfg-${k}" value='${JSON.stringify(v)}'
             style="font-family:'IBM Plex Mono',monospace;font-size:11.5px">`
    f.appendChild(g)
  }
}
async function saveCfg(){
  const keys=Object.keys(await fetch('/api/config').then(r=>r.json()))
  const p={}
  for(const k of keys){const el=document.getElementById('cfg-'+k);if(!el)continue;try{p[k]=JSON.parse(el.value)}catch{p[k]=el.value}}
  const res=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}).then(r=>r.json())
  const st=document.getElementById('cfg-status')
  st.textContent=res.ok?'✔ Saved':'✖ Failed'; st.className='cfg-status '+(res.ok?'ok':'')
  toast(st.textContent,res.ok?'pass':'fail')
}

// ── 40-Pin Map ─────────────────────────────────────────────────────────────────
let _pins=null
async function buildPinMap(){
  const wrap=document.getElementById('pin-map-table')
  if(wrap.innerHTML) return
  if(!_pins) _pins=(await fetch('/api/pinmap').then(r=>r.json())).pins

  // Legend
  const legWrap=document.getElementById('pin-legend')
  const types={PWR33:'3.3V',PWR5:'5V',GND:'GND',GPIO:'GPIO',I2C:'I2C',SPI:'SPI',UART:'UART',PWM:'PWM',ONEWIRE:'1-Wire',ID:'HAT-ID'}
  Object.entries(types).forEach(([t,n])=>{
    const span=document.createElement('span')
    span.className='legend-item pt-'+t; span.textContent='■ '+n
    span.onclick=()=>filterPins(t)
    legWrap.appendChild(span)
  })

  for(let i=0;i<_pins.length;i+=2){
    const L=_pins[i], R=_pins[i+1]
    const lToggle=L.type==='GPIO'?`<button class="pin-toggle-btn low" id="ptb-${L.bcm}" onclick="pinToggle(${L.bcm},this)" title="Toggle GPIO${L.bcm}">LOW</button>`:''
    const rToggle=R.type==='GPIO'?`<button class="pin-toggle-btn low" id="ptb-${R.bcm}" onclick="pinToggle(${R.bcm},this)" title="Toggle GPIO${R.bcm}">LOW</button>`:''
    const tr=document.createElement('tr')
    tr.innerHTML=`
      <td class="pin-cell-left pin-desc" title="${L.desc}">${L.desc}</td>
      <td class="pin-cell-left"><span class="pin-label pt-${L.type}" onclick="pinDetail(${L.phys})">${L.label}</span>${lToggle}</td>
      <td><div class="pin-num pn-${L.type}" onclick="pinDetail(${L.phys})" title="Pin ${L.phys}">${L.phys}</div></td>
      <td class="pin-sep">│</td>
      <td><div class="pin-num pn-${R.type}" onclick="pinDetail(${R.phys})" title="Pin ${R.phys}">${R.phys}</div></td>
      <td><span class="pin-label pt-${R.type}" onclick="pinDetail(${R.phys})">${R.label}</span>${rToggle}</td>
      <td class="pin-cell-right pin-desc" title="${R.desc}">${R.desc}</td>`
    wrap.appendChild(tr)
  }
}

async function pinToggle(bcm, btn){
  event.stopPropagation()
  const isHigh=btn.classList.contains('high')
  const newVal=isHigh?0:1
  const res=await fetch('/api/gpio/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:bcm,value:newVal})}).then(r=>r.json())
  if(res.ok){
    btn.textContent=newVal?'HIGH':'LOW'
    btn.className='pin-toggle-btn '+(newVal?'high':'low')
    toast(`GPIO${bcm} → ${newVal?'HIGH':'LOW'}`,newVal?'pass':'info')
  } else {
    toast('✖ '+res.msg,'fail')
  }
}

let _pinFilter=null
function filterPins(type){
  _pinFilter=_pinFilter===type?null:type
  document.querySelectorAll('#pin-map-table tr').forEach((tr,i)=>{
    const L=_pins[i*2], R=_pins[i*2+1]
    if(!_pinFilter) tr.style.opacity='1'
    else tr.style.opacity=(L.type===_pinFilter||R.type===_pinFilter)?'1':'0.15'
  })
}

function _altClass(fn){
  if(!fn||fn==='—') return 'ach-none'
  const fnUpper = fn.toUpperCase()
  if(fnUpper.includes('UART')||fnUpper.includes('TXD')||fnUpper.includes('RXD')||
     fnUpper.includes('CTS')||fnUpper.includes('RTS')) return 'ach-uart'
  if(fnUpper.includes('SPI'))  return 'ach-spi'
  if(fnUpper.includes('I2C')||fnUpper.includes('SDA')||fnUpper.includes('SCL')) return 'ach-i2c'
  if(fnUpper.includes('PWM'))  return 'ach-pwm'
  if(fnUpper.includes('PCM'))  return 'ach-pcm'
  if(fnUpper.includes('JTAG')||fnUpper.includes('ARM')) return 'ach-jtag'
  if(fnUpper.includes('CLK')||fnUpper.includes('GPCLK')||fnUpper.includes('PCLK')) return 'ach-clk'
  if(fnUpper.includes('DPI'))  return 'ach-dpi'
  if(fnUpper.includes('SD'))   return 'ach-dpi'
  return ''
}

function pinDetail(phys){
  const pin=_pins.find(p=>p.phys===phys); if(!pin) return
  const panel=document.getElementById('pin-detail-panel')
  const cont=document.getElementById('pin-detail-content')
  panel.className='pin-detail-panel visible'
  const bcmStr=pin.bcm!==null?`BCM ${pin.bcm}`:'No BCM'
  const code=pin.bcm!==null?`
    <div class="pd-code">GPIO.setup(${pin.bcm}, GPIO.OUT)  # or GPIO.IN<br>GPIO.output(${pin.bcm}, GPIO.HIGH)<br>GPIO.output(${pin.bcm}, GPIO.LOW)<br>val = GPIO.input(${pin.bcm})</div>`:''
  
  // Build alternate functions display
  let altsHtml=''
  if(pin.alts && typeof pin.alts === 'object' && Object.keys(pin.alts).length > 0){
    const rows = Object.entries(pin.alts)
      .filter(([_, v]) => v && v.trim() !== '')
      .map(([k, v]) => `<tr><td>${k}</td><td><span class="alt-chip ${_altClass(v)}">${v}</span></td></tr>`)
      .join('')
    if(rows){
      altsHtml = `<div class="pd-alts"><div class="pd-alts-title">Alternate Functions (ALT0–ALT5)</div>
        <table class="alt-table">${rows}</table></div>`
    } else {
      altsHtml = `<div class="pd-alts"><div class="pd-alts-title">Alternate Functions</div>
        <div style="color:var(--text3);font-size:11px;padding:8px 0">No alternate functions defined for this pin</div></div>`
    }
  } else {
    altsHtml = `<div class="pd-alts"><div class="pd-alts-title">Alternate Functions</div>
      <div style="color:var(--text3);font-size:11px;padding:8px 0">No alternate functions defined for this pin</div></div>`
  }
  
  cont.innerHTML=`
    <div class="pd-header">
      <span class="pd-badge pt-${pin.type}">${pin.label}</span>
      <span style="font-size:14px;font-weight:700">Pin ${pin.phys}</span>
      <span style="color:var(--text3);font-size:12px">${bcmStr} · ${pin.type}</span>
    </div>
    <div style="font-size:12px;color:var(--text2);margin-bottom:6px">${pin.desc}</div>
    ${code}${altsHtml}
  `
  panel.scrollIntoView({behavior:'smooth',block:'nearest'})
}

// ── Alt Functions Full Table ───────────────────────────────────────────────────
async function buildAltTable(){
  const tbl=document.getElementById('alt-fn-table')
  if(tbl.innerHTML) return
  if(!_pins) _pins=(await fetch('/api/pinmap').then(r=>r.json())).pins

  // Attribution banner
  const wrap=tbl.closest('.card')
  const attr=document.createElement('div')
  attr.style.cssText='padding:10px 16px 0;font-size:10px;color:var(--text3);font-family:"IBM Plex Mono",monospace;border-bottom:1px solid var(--border);margin-bottom:0'
  attr.innerHTML='<strong style="color:var(--text2)">Table 5 — Raspberry Pi 4 GPIO Alternate Functions</strong> &nbsp;·&nbsp; Source: RP-008341-DS Release 1.1, March 2024 &nbsp;·&nbsp; © Raspberry Pi (Trading) Ltd. &nbsp;·&nbsp; BCM2711 GPIO0–GPIO27'
  wrap.insertBefore(attr,tbl)

  const thead=document.createElement('thead')
  thead.innerHTML='<tr><th>GPIO</th><th>Pull</th><th>Phys</th><th>ALT0</th><th>ALT1</th><th>ALT2</th><th>ALT3</th><th>ALT4</th><th>ALT5</th></tr>'
  tbl.appendChild(thead)

  // Sort GPIO0 first, GPIO27 last
  const sorted=[..._pins].filter(p=>p.bcm!==null&&p.alts&&Object.keys(p.alts).length)
                          .sort((a,b)=>a.bcm-b.bcm)

  const tbody=document.createElement('tbody')
  sorted.forEach(p=>{
    const pull=p.desc.includes('pull=High')?'High':p.desc.includes('pull=Low')?'Low':'—'
    const pullCls=pull==='High'?'hpull-hi':pull==='Low'?'hpull-lo':''
    const alts=['ALT0','ALT1','ALT2','ALT3','ALT4','ALT5']
    const altCells=alts.map(a=>{
      const v=p.alts[a]||''
      if(!v||v==='—') return '<td><span class="ach-none">—</span></td>'
      return `<td><span class="alt-chip ${_altClass(v)}">${v}</span></td>`
    }).join('')
    const tr=document.createElement('tr')
    tr.innerHTML=`
      <td class="col-pin"><span class="pin-label pt-${p.type}" style="cursor:default;font-size:10px">GPIO${p.bcm}</span></td>
      <td><span class="pull-badge ${pullCls}">${pull}</span></td>
      <td style="color:var(--text3);font-size:10px">P${p.phys}</td>
      ${altCells}`
    tbody.appendChild(tr)
  })
  tbl.appendChild(tbody)

  // Peripheral summary
  const summ=document.createElement('div')
  summ.style.cssText='padding:14px 16px;border-top:1px solid var(--border);font-size:11px'
  summ.innerHTML=`
    <div style="font-weight:700;margin-bottom:8px;color:var(--text2)">BCM2711 Peripheral Summary (Pi 4 only)</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:5px">
      <div><span class="alt-chip ach-uart" style="margin-right:6px">UART</span>UART0–UART5 &nbsp;(6 UARTs, UART0+1 console-capable)</div>
      <div><span class="alt-chip ach-spi"  style="margin-right:6px">SPI</span>SPI0–SPI6 &nbsp;(7 buses, SPI0 on header)</div>
      <div><span class="alt-chip ach-i2c"  style="margin-right:6px">I2C</span>I2C0–I2C6 &nbsp;(7 buses, I2C0 HAT EEPROM, I2C1 header)</div>
      <div><span class="alt-chip ach-pwm"  style="margin-right:6px">PWM</span>PWM0+1 &nbsp;(GPIO12/18=PWM0, GPIO13/19=PWM1)</div>
      <div><span class="alt-chip ach-pcm"  style="margin-right:6px">PCM</span>PCM CLK/FS/DIN/DOUT &nbsp;(I2S audio)</div>
      <div><span class="alt-chip ach-clk"  style="margin-right:6px">GPCLK</span>GPCLK0/1/2 &nbsp;(3 programmable clock outputs)</div>
      <div><span class="alt-chip ach-dpi"  style="margin-right:6px">DPI</span>D0–D23 &nbsp;(24-bit parallel RGB display)</div>
      <div><span class="alt-chip ach-jtag" style="margin-right:6px">ARM</span>JTAG &nbsp;(TRST/RTCK/TDO/TCK/TDI/TMS debug)</div>
    </div>`
  wrap.appendChild(summ)
}

// ── Backends ──────────────────────────────────────────────────────────────────
function buildBackends(){
  const list=document.getElementById('backends-list')
  if(list.innerHTML) return
  const bkds=[
    {name:'RPi.GPIO',env:'rpigpio',install:'pip install RPi.GPIO',info:'Default for Pi 1–4. Not supported on Pi 5. Edge detection + software PWM.',api:'GPIO.setup(), GPIO.output(), GPIO.PWM(), add_event_detect()'},
    {name:'pigpio',env:'pigpio',install:'pip install pigpio + sudo systemctl start pigpiod',info:'Hardware-timed PWM, servo, bit-bang UART, remote GPIO. Best timing accuracy.',api:'pi.write(), pi.set_servo_pulsewidth(), bb_serial_read()'},
    {name:'lgpio',env:'lgpio',install:'pip install lgpio',info:'Modern replacement for RPi.GPIO. Pi 5 compatible. Hardware PWM.',api:'lgpio.gpio_write(), lgpio.tx_pwm()'},
    {name:'gpiod',env:'gpiod',install:'pip install gpiod (libgpiod ≥ 2.x)',info:'Kernel character device. No root required. Software PWM via threading.',api:"chip.get_line(), line.set_value()"},
    {name:'gpiozero',env:'gpiozero',install:'pip install gpiozero',info:'High-level abstraction. Best for rapid prototyping.',api:'LED(), Button(), PWMLED(), MCP3008()'},
    {name:'smbus2',env:'—',install:'pip install smbus2',info:'Pure-Python SMBus/I2C. No GPIO dependency. Register read/write.',api:'SMBus(1).read_byte(addr), read_byte_data(addr, reg)'},
    {name:'busio',env:'—',install:'pip install adafruit-blinka',info:'CircuitPython I2C/SPI/UART (Blinka). Cross-platform.',api:'busio.I2C(), busio.SPI(), busio.UART()'},
  ]
  bkds.forEach(b=>{
    const card=document.createElement('div'); card.className='card'
    card.innerHTML=`
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <code style="font-size:14px;font-weight:700;font-family:'IBM Plex Mono',monospace;color:var(--blue)">${b.name}</code>
        ${b.env!=='—'?`<code style="background:var(--blue-lt);color:var(--blue);border:1px solid var(--blue-bd);padding:2px 8px;border-radius:4px;font-size:10px;font-family:'IBM Plex Mono',monospace">RPI_GPIO_BACKEND=${b.env}</code>`:''}
      </div>
      <div style="font-size:11.5px;color:var(--text2);margin-bottom:8px">${b.info}</div>
      <div style="display:grid;grid-template-columns:60px 1fr;gap:3px;font-size:11.5px">
        <span style="color:var(--text3);font-weight:600">Install</span>
        <code style="background:var(--bg3);padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:10.5px">${b.install}</code>
        <span style="color:var(--text3);font-weight:600">Key API</span>
        <code style="background:var(--bg3);padding:2px 8px;border-radius:4px;font-family:'IBM Plex Mono',monospace;font-size:10.5px">${b.api}</code>
      </div>`
    list.appendChild(card)
  })
}
</script>
</body>
</html>"""

@app.route("/")
def index(): return HTML

if __name__ == "__main__":
    port = 5000
    if "--port" in sys.argv:
        try: port = int(sys.argv[sys.argv.index("--port")+1])
        except (IndexError,ValueError): pass

    print("\n  🍓 RPi Hardware Test — Web UI  v2.1")
    print("  " + "─"*40)
    print(f"  Local  :  http://127.0.0.1:{port}")
    try:
        ip = subprocess.check_output("hostname -I",shell=True,text=True).split()[0]
        print(f"  Network:  http://{ip}:{port}")
    except Exception: pass
    print(f"  Backend:  {core.GPIO_ADAPTER.name}")
    print("  Stop   :  Ctrl+C\n")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
