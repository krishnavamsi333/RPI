#!/usr/bin/env python3
"""
rpi_web.py — Professional Flask Web UI for Raspberry Pi Hardware Test Suite
White theme, side-by-side pin map (1|2, 3|4...), full feature set.

Usage:
    sudo python3 rpi_web.py              # http://0.0.0.0:5000
    sudo python3 rpi_web.py --port 8080

Open in browser:  http://<pi-ip>:5000
Find your Pi IP:  hostname -I
"""

import io
import os
import re
import sys
import json
import time
import queue
import threading
import subprocess
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

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def _strip_ansi(s):
    return _ANSI_RE.sub("", s)

def _classify(line):
    c = line.strip()
    if "✔ PASS" in c or ("PASS" in c and "[INFO]" in c): return "pass"
    if "✖ FAIL" in c or ("FAIL" in c and "[INFO]" in c): return "fail"
    if c.startswith("⚠") or "warn" in c.lower(): return "warn"
    if c.startswith("ℹ"): return "info"
    if any(c.startswith(x) for x in ("═","╔","╚","╠","┌","└","─")): return "header"
    return "normal"

class _QueueWriter(io.TextIOBase):
    def __init__(self, q): self._q = q
    def write(self, s):
        if s: self._q.put(s)
        return len(s)
    def flush(self): pass

def _run_and_stream(fn, *args):
    q = queue.Queue()
    sentinel = object()
    def _worker():
        writer = _QueueWriter(q)
        try:
            with redirect_stdout(writer):
                fn(*args)
        except Exception as e:
            q.put(f"\n  ✖ FAIL  Exception: {e}\n")
        finally:
            q.put(sentinel)
    threading.Thread(target=_worker, daemon=True).start()
    buf = ""
    while True:
        try:
            chunk = q.get(timeout=30)
        except queue.Empty:
            yield "data: [TIMEOUT]\n\n"; break
        if chunk is sentinel:
            yield "data: [DONE]\n\n"; break
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            clean = _strip_ansi(line)
            if clean.strip():
                yield f"data: {json.dumps({'line': clean, 'cls': _classify(clean)})}\n\n"
    if buf.strip():
        clean = _strip_ansi(buf)
        yield f"data: {json.dumps({'line': clean, 'cls': _classify(clean)})}\n\n"
    _test_active.clear()

def _stream(fn, *args):
    def gen():
        if not _test_lock.acquire(blocking=False):
            yield 'data: {"line":"⚠ Another test is running — please wait","cls":"warn"}\n\n'
            yield "data: [DONE]\n\n"; return
        _test_active.set()
        try:
            yield from _run_and_stream(fn, *args)
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
def api_gpio():     return _stream(core.gpio_test)
@app.route("/api/test/gpio_irq")  
def api_gpio_irq(): return _stream(core.gpio_interrupt_test)
@app.route("/api/test/pwm")       
def api_pwm():      return _stream(core.pwm_test)
@app.route("/api/test/servo")     
def api_servo():    return _stream(core.pwm_servo_test)
@app.route("/api/test/i2c")       
def api_i2c():      return _stream(core.i2c_test)
@app.route("/api/test/spi")       
def api_spi():      return _stream(core.spi_test)
@app.route("/api/test/uart")      
def api_uart():     return _stream(core.uart_test)
@app.route("/api/test/all")       
def api_all():      return _stream(core.run_all)
@app.route("/api/test/deps")      
def api_deps():     return _stream(core.check_deps)
@app.route("/api/test/sysinfo")   
def api_sysinfo():  return _stream(core.system_info)
@app.route("/api/test/snapshot")  
def api_snapshot(): return _stream(core.gpio_snapshot)

# ── GPIO Control ───────────────────────────────────────────────────────────────
@app.route("/api/gpio/set", methods=["POST"])
def api_gpio_set():
    d = request.get_json(force=True)
    bcm = int(d.get("pin", -1))
    val = int(d.get("value", 0))
    if not (0 <= bcm <= 27):
        return jsonify(ok=False, msg="Invalid BCM pin")
    try:
        core.GPIO_ADAPTER.setup(bcm, "out")
        core.GPIO_ADAPTER.output(bcm, val)
        return jsonify(ok=True, msg=f"GPIO{bcm} → {'HIGH' if val else 'LOW'}")
    except Exception as e:
        return jsonify(ok=False, msg=str(e))

@app.route("/api/gpio/read", methods=["POST"])
def api_gpio_read():
    d = request.get_json(force=True)
    bcm = int(d.get("pin", -1))
    if not (0 <= bcm <= 27):
        return jsonify(ok=False, msg="Invalid BCM pin")
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
        {"phys":p,"bcm":b,"label":l,"type":t,"desc":d}
        for p,b,l,t,d in core.PIN_MAP
    ])

@app.route("/api/config", methods=["GET"])
def api_config_get(): return jsonify(core.CFG)

@app.route("/api/config", methods=["POST"])
def api_config_set():
    for k, v in request.get_json(force=True).items():
        if k in core.CFG: core.CFG[k] = v
    with open(core.CONFIG_FILE, "w") as f: json.dump(core.CFG, f, indent=2)
    return jsonify(ok=True, config=core.CFG)

@app.route("/api/log")
def api_log():
    lines = int(request.args.get("lines", 60))
    try:
        with open(core._LOG_FILE) as f:
            tail = f.readlines()[-lines:]
        return jsonify(lines=[_strip_ansi(l.rstrip()) for l in tail])
    except FileNotFoundError:
        return jsonify(lines=[])

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
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RPi Hardware Test Suite</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #f8f9fb;
  --bg2:      #ffffff;
  --bg3:      #f2f4f7;
  --bg4:      #e8ecf0;
  --border:   #e2e6eb;
  --border2:  #cdd2d8;
  --text:     #0d1117;
  --text2:    #4a5568;
  --text3:    #8a9ab0;
  --text4:    #b0bcc8;

  --blue:     #2563eb;
  --blue-lt:  #eff4ff;
  --blue-bd:  #c7d7fd;
  --green:    #16a34a;
  --green-lt: #f0fdf4;
  --green-bd: #bbf7d0;
  --red:      #dc2626;
  --red-lt:   #fff1f1;
  --red-bd:   #fecaca;
  --yellow:   #d97706;
  --yel-lt:   #fffbeb;
  --yel-bd:   #fde68a;
  --purple:   #7c3aed;
  --pur-lt:   #f5f3ff;
  --cyan:     #0891b2;
  --cyn-lt:   #ecfeff;

  --shadow-sm: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  --shadow:    0 4px 12px rgba(0,0,0,.06), 0 2px 4px rgba(0,0,0,.04);
  --shadow-lg: 0 10px 30px rgba(0,0,0,.08), 0 4px 10px rgba(0,0,0,.05);
  --r:   8px;
  --r2:  12px;
  --r3:  16px;
}

* { box-sizing:border-box; margin:0; padding:0 }
html { scroll-behavior:smooth }
body {
  background:var(--bg);
  color:var(--text);
  font-family:'DM Sans',sans-serif;
  font-size:14px;
  line-height:1.6;
  min-height:100vh;
}

/* ── Layout ── */
.shell { display:grid; grid-template-columns:260px 1fr; min-height:100vh }

/* ── Sidebar ── */
.sidebar {
  background:var(--bg2);
  border-right:1px solid var(--border);
  position:sticky;
  top:0; height:100vh;
  overflow-y:auto;
  display:flex; flex-direction:column;
  padding-bottom:16px;
}
.brand {
  padding:22px 20px 16px;
  border-bottom:1px solid var(--border);
}
.brand-logo {
  display:flex; align-items:center; gap:10px;
  margin-bottom:6px;
}
.brand-icon {
  width:36px; height:36px;
  background:linear-gradient(135deg,#dc2626,#f97316);
  border-radius:10px;
  display:flex; align-items:center; justify-content:center;
  font-size:18px;
  box-shadow:0 4px 12px rgba(220,38,38,.25);
}
.brand-title { font-size:16px; font-weight:700; letter-spacing:-.02em }
.brand-sub { font-size:11px; color:var(--text3); margin-bottom:10px }
.backend-pill {
  display:inline-flex; align-items:center; gap:6px;
  background:var(--bg3);
  border:1px solid var(--border);
  border-radius:20px;
  padding:4px 10px;
  font-size:11px;
  font-family:'DM Mono',monospace;
  color:var(--blue);
  font-weight:500;
}
.bp-dot {
  width:6px; height:6px; border-radius:50%;
  background:var(--green);
  box-shadow:0 0 0 2px rgba(22,163,74,.2);
  animation:breathe 2s ease-in-out infinite;
}
@keyframes breathe { 0%,100%{opacity:1} 50%{opacity:.4} }

.nav-section {
  padding:16px 12px 4px;
  font-size:10px;
  font-weight:700;
  letter-spacing:.1em;
  text-transform:uppercase;
  color:var(--text4);
}
.nav-btn {
  display:flex; align-items:center; gap:10px;
  width:calc(100% - 16px);
  margin:2px 8px;
  padding:9px 12px;
  background:none;
  border:none;
  color:var(--text2);
  cursor:pointer;
  font-size:13px;
  font-family:'DM Sans',sans-serif;
  font-weight:500;
  text-align:left;
  border-radius:var(--r);
  transition:all .15s;
}
.nav-btn:hover { background:var(--bg3); color:var(--text) }
.nav-btn.active {
  background:var(--blue-lt);
  color:var(--blue);
  box-shadow:inset 2px 0 0 var(--blue);
}
.nav-ic { width:18px; text-align:center; flex-shrink:0; font-size:15px }
.nav-tag {
  margin-left:auto;
  font-size:10px;
  padding:2px 6px;
  border-radius:4px;
  background:var(--bg4);
  color:var(--text3);
  font-weight:600;
}
.nav-run-tag {
  margin-left:auto;
  font-size:10px;
  padding:2px 6px;
  border-radius:4px;
  background:var(--yel-lt);
  color:var(--yellow);
  border:1px solid var(--yel-bd);
  font-weight:600;
  animation:pulse2 1s infinite;
}
@keyframes pulse2 { 0%,100%{opacity:1} 50%{opacity:.3} }

.sb-footer {
  margin-top:auto;
  padding:14px 16px;
  border-top:1px solid var(--border);
  font-size:11px;
  color:var(--text3);
  display:flex; align-items:center; gap:6px;
}

/* ── Main Content ── */
.main { padding:28px 32px; overflow-x:hidden; min-width:0 }

/* ── Page ── */
.page { display:none }
.page.active { display:block; animation:fadeIn .2s ease }
@keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
.page-header { margin-bottom:22px }
.page-title { font-size:22px; font-weight:700; letter-spacing:-.03em; margin-bottom:3px }
.page-sub { color:var(--text3); font-size:13px }

/* ── Cards ── */
.card {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:var(--r3);
  padding:18px 20px;
  margin-bottom:16px;
  box-shadow:var(--shadow-sm);
}
.card-title {
  font-size:10px;
  font-weight:700;
  letter-spacing:.1em;
  text-transform:uppercase;
  color:var(--text3);
  margin-bottom:12px;
}

/* ── Test Cards Grid ── */
.test-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(175px,1fr));
  gap:12px;
  margin-bottom:20px;
}
.test-card {
  background:var(--bg2);
  border:2px solid var(--border);
  border-radius:var(--r3);
  padding:16px;
  cursor:pointer;
  transition:all .18s;
  position:relative;
  overflow:hidden;
}
.test-card::before {
  content:'';
  position:absolute;
  top:0; left:0; right:0;
  height:3px;
  background:var(--border);
  transition:background .18s;
}
.test-card:hover {
  border-color:var(--blue-bd);
  box-shadow:var(--shadow);
  transform:translateY(-1px);
}
.test-card:hover::before { background:var(--blue) }
.test-card.running { border-color:var(--yel-bd) }
.test-card.running::before { background:var(--yellow) }
.test-card.pass { border-color:var(--green-bd) }
.test-card.pass::before { background:var(--green) }
.test-card.fail { border-color:var(--red-bd) }
.test-card.fail::before { background:var(--red) }
.tc-icon { font-size:24px; margin-bottom:8px }
.tc-name { font-size:13px; font-weight:600; margin-bottom:3px; color:var(--text) }
.tc-desc { font-size:11px; color:var(--text3) }
.tc-badge {
  position:absolute; top:10px; right:10px;
  font-size:10px; font-weight:700;
  padding:2px 8px; border-radius:20px;
  letter-spacing:.04em;
}
.tc-badge.pass { background:var(--green-lt); color:var(--green); border:1px solid var(--green-bd) }
.tc-badge.fail { background:var(--red-lt); color:var(--red); border:1px solid var(--red-bd) }
.tc-badge.running { background:var(--yel-lt); color:var(--yellow); border:1px solid var(--yel-bd) }

/* ── Terminal ── */
.term-wrap { margin-top:8px }
.term-header {
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:6px;
}
.term-label {
  display:flex; align-items:center; gap:7px;
  font-size:11px; color:var(--text3); font-family:'DM Mono',monospace;
}
.term-dot {
  width:7px; height:7px; border-radius:50%; background:var(--green-bd);
  animation:breathe 2s ease-in-out infinite;
}
.term {
  background:#fafbfc;
  border:1px solid var(--border);
  border-radius:var(--r2);
  font-family:'DM Mono',monospace;
  font-size:12px;
  line-height:1.7;
  padding:14px 16px;
  min-height:160px;
  max-height:420px;
  overflow-y:auto;
  white-space:pre-wrap;
  word-break:break-all;
}
.term .pass   { color:var(--green) }
.term .fail   { color:var(--red) }
.term .warn   { color:var(--yellow) }
.term .info   { color:var(--blue) }
.term .header { color:var(--purple); font-weight:600 }
.term .normal { color:var(--text2) }

/* ── Buttons ── */
.btn {
  display:inline-flex; align-items:center; gap:7px;
  padding:8px 16px;
  border-radius:var(--r);
  border:1px solid var(--border);
  background:var(--bg2);
  color:var(--text);
  cursor:pointer;
  font-size:13px;
  font-family:'DM Sans',sans-serif;
  font-weight:500;
  transition:all .14s;
  box-shadow:var(--shadow-sm);
}
.btn:hover { background:var(--bg3); border-color:var(--border2) }
.btn:disabled { opacity:.4; cursor:not-allowed }
.btn-primary {
  background:var(--blue); border-color:var(--blue);
  color:#fff; box-shadow:0 2px 8px rgba(37,99,235,.3);
}
.btn-primary:hover { background:#1d4ed8; border-color:#1d4ed8 }
.btn-success {
  background:var(--green); border-color:var(--green);
  color:#fff; box-shadow:0 2px 8px rgba(22,163,74,.3);
}
.btn-success:hover { background:#15803d }
.btn-danger {
  background:var(--red); border-color:var(--red);
  color:#fff;
}
.btn-run-all {
  width:100%; justify-content:center;
  padding:12px; font-size:15px; font-weight:600;
  background:linear-gradient(135deg,#2563eb,#7c3aed);
  border-color:transparent; color:#fff;
  border-radius:var(--r2);
  box-shadow:0 4px 16px rgba(37,99,235,.3);
  margin-bottom:18px;
  letter-spacing:-.01em;
}
.btn-run-all:hover {
  background:linear-gradient(135deg,#1d4ed8,#6d28d9);
  transform:translateY(-1px);
  box-shadow:0 6px 20px rgba(37,99,235,.4);
}
.clr-btn {
  background:none; border:1px solid var(--border);
  border-radius:6px; color:var(--text3);
  cursor:pointer; padding:3px 10px; font-size:11px;
  font-family:'DM Sans',sans-serif;
  transition:all .13s;
}
.clr-btn:hover { color:var(--text); border-color:var(--border2) }

/* ── Wiring box ── */
.wiring {
  background:var(--bg3);
  border:1px solid var(--border);
  border-left:3px solid var(--blue);
  border-radius:0 var(--r) var(--r) 0;
  padding:12px 16px;
  font-family:'DM Mono',monospace;
  font-size:12px;
  color:var(--text2);
  line-height:2;
  margin-bottom:14px;
}
.wiring-title { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.08em; color:var(--blue); margin-bottom:6px }

/* ── Stat grid ── */
.stat-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(140px,1fr));
  gap:12px;
  margin-bottom:18px;
}
.stat-card {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:var(--r2);
  padding:14px 16px;
  box-shadow:var(--shadow-sm);
}
.stat-label { font-size:10px; color:var(--text3); font-weight:600; text-transform:uppercase; letter-spacing:.06em; margin-bottom:5px }
.stat-val { font-size:20px; font-weight:700; font-family:'DM Mono',monospace }
.sv-blue   { color:var(--blue) }
.sv-green  { color:var(--green) }
.sv-red    { color:var(--red) }
.sv-yellow { color:var(--yellow) }

/* ── 40-Pin Map ── */
.pin-map-wrap { overflow-x:auto }
.pin-map-table {
  border-collapse:separate;
  border-spacing:4px 5px;
  font-size:12px;
  font-family:'DM Mono',monospace;
  margin:0 auto;
}
.pin-map-table td { vertical-align:middle; padding:0 }
.pin-cell-left  { text-align:right }
.pin-cell-right { text-align:left }
.pin-label {
  display:inline-flex; align-items:center;
  padding:4px 10px;
  border-radius:6px;
  font-weight:500;
  font-size:11px;
  white-space:nowrap;
  cursor:pointer;
  transition:all .13s;
}
.pin-label:hover { filter:brightness(.93) }
.pin-num {
  width:26px; height:26px;
  border-radius:50%;
  display:flex; align-items:center; justify-content:center;
  font-size:10px; font-weight:700;
  cursor:pointer;
  transition:transform .13s;
  border:2px solid rgba(255,255,255,.4);
  color:#fff;
  flex-shrink:0;
}
.pin-num:hover { transform:scale(1.2); box-shadow:0 2px 8px rgba(0,0,0,.2) }
.pin-sep {
  width:20px; text-align:center;
  color:var(--border2); font-size:16px;
}
.pin-desc {
  color:var(--text3);
  font-size:10px;
  max-width:130px;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
  padding:0 8px;
}

/* Pin type colors */
.pt-PWR33 { background:#fef2f2; color:#991b1b; border:1px solid #fecaca }
.pn-PWR33 { background:#dc2626 }
.pt-PWR5  { background:#fff7ed; color:#9a3412; border:1px solid #fed7aa }
.pn-PWR5  { background:#ea580c }
.pt-GND   { background:#f9fafb; color:#374151; border:1px solid #d1d5db }
.pn-GND   { background:#6b7280 }
.pt-GPIO  { background:#eff6ff; color:#1e40af; border:1px solid #bfdbfe }
.pn-GPIO  { background:#2563eb }
.pt-I2C   { background:#f0fdf4; color:#14532d; border:1px solid #bbf7d0 }
.pn-I2C   { background:#16a34a }
.pt-SPI   { background:#fff7ed; color:#7c2d12; border:1px solid #fed7aa }
.pn-SPI   { background:#c2410c }
.pt-UART  { background:#fefce8; color:#713f12; border:1px solid #fde68a }
.pn-UART  { background:#d97706 }
.pt-PWM   { background:#faf5ff; color:#581c87; border:1px solid #e9d5ff }
.pn-PWM   { background:#7c3aed }
.pt-ID    { background:#f8fafc; color:#475569; border:1px solid #cbd5e1 }
.pn-ID    { background:#64748b }

/* Legend */
.pin-legend {
  display:flex; flex-wrap:wrap; gap:6px;
  margin-bottom:16px;
}
.legend-item {
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 10px;
  border-radius:20px;
  font-size:11px; font-weight:500;
}

/* ── Pin detail panel ── */
.pin-detail-panel {
  background:var(--blue-lt);
  border:1px solid var(--blue-bd);
  border-radius:var(--r2);
  padding:16px 18px;
  margin-top:14px;
  display:none;
}
.pin-detail-panel.visible { display:block; animation:fadeIn .2s ease }
.pd-header { display:flex; align-items:center; gap:10px; margin-bottom:10px }
.pd-badge { padding:5px 12px; border-radius:6px; font-size:13px; font-weight:600 }
.pd-code {
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:var(--r);
  padding:10px 14px;
  font-family:'DM Mono',monospace;
  font-size:12px;
  color:var(--blue);
  margin-top:8px;
  line-height:1.8;
}

/* ── Manual GPIO ── */
.gpio-control-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:16px }
.form-group { display:flex; flex-direction:column; gap:5px }
.form-group label { font-size:11px; color:var(--text2); font-weight:600; text-transform:uppercase; letter-spacing:.05em }
.form-input {
  background:var(--bg3);
  border:1px solid var(--border);
  border-radius:var(--r);
  color:var(--text);
  padding:8px 11px;
  font-size:13px;
  font-family:'DM Sans',sans-serif;
  transition:border-color .13s;
  width:100%;
}
.form-input:focus { outline:none; border-color:var(--blue); background:var(--bg2) }
select.form-input { cursor:pointer }
.gpio-result {
  margin-top:10px;
  padding:10px 14px;
  border-radius:var(--r);
  font-family:'DM Mono',monospace;
  font-size:13px;
  font-weight:500;
  display:none;
}
.gpio-result.pass { background:var(--green-lt); border:1px solid var(--green-bd); color:var(--green) }
.gpio-result.fail { background:var(--red-lt); border:1px solid var(--red-bd); color:var(--red) }

/* ── Config form ── */
.cfg-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px }
.cfg-status { font-size:12px; color:var(--text3); margin-left:12px }
.cfg-status.ok { color:var(--green) }

/* ── Log viewer ── */
.log-line {
  padding:3px 0;
  border-bottom:1px solid var(--bg3);
  font-size:11.5px;
  font-family:'DM Mono',monospace;
  color:var(--text2);
}
.log-line.pass { color:var(--green) }
.log-line.fail { color:var(--red) }
.log-line.warn { color:var(--yellow) }

/* ── Toast ── */
.toast {
  position:fixed; bottom:24px; right:24px;
  background:var(--bg2);
  border:1px solid var(--border);
  border-radius:var(--r2);
  padding:11px 16px;
  font-size:13px; font-weight:500;
  box-shadow:var(--shadow-lg);
  z-index:9999;
  transition:all .3s;
  display:flex; align-items:center; gap:8px;
}
.toast.hidden { opacity:0; transform:translateY(8px); pointer-events:none }
.toast.pass { border-color:var(--green-bd); background:var(--green-lt); color:var(--green) }
.toast.fail { border-color:var(--red-bd); background:var(--red-lt); color:var(--red) }
.toast.info { border-color:var(--blue-bd); background:var(--blue-lt); color:var(--blue) }

/* ── Divider ── */
.divider { height:1px; background:var(--border); margin:16px 0 }

/* ── Responsive ── */
@media(max-width:720px) {
  .shell { grid-template-columns:1fr }
  .sidebar { position:static; height:auto }
  .main { padding:16px }
  .gpio-control-grid, .cfg-grid { grid-template-columns:1fr }
}

::-webkit-scrollbar { width:5px; height:5px }
::-webkit-scrollbar-track { background:transparent }
::-webkit-scrollbar-thumb { background:var(--border2); border-radius:3px }
::-webkit-scrollbar-thumb:hover { background:var(--text4) }
</style>
</head>
<body>
<div class="shell">

<!-- ═══════════════ SIDEBAR ═══════════════ -->
<nav class="sidebar">
  <div class="brand">
    <div class="brand-logo">
      <div class="brand-icon">🍓</div>
      <div>
        <div class="brand-title">RPi Test Suite</div>
      </div>
    </div>
    <div class="brand-sub">Hardware diagnostic dashboard</div>
    <div class="backend-pill">
      <span class="bp-dot"></span>
      <span id="backend-name">loading…</span>
    </div>
  </div>

  <div class="nav-section">Tests</div>
  <button class="nav-btn active" onclick="sp('dashboard')" id="nav-dashboard">
    <span class="nav-ic">⊞</span>Dashboard
  </button>
  <button class="nav-btn" onclick="sp('gpio')" id="nav-gpio">
    <span class="nav-ic">⚡</span>GPIO
    <span class="nav-tag">loopback</span>
  </button>
  <button class="nav-btn" onclick="sp('pwm')" id="nav-pwm">
    <span class="nav-ic">〜</span>PWM
    <span class="nav-tag">LED+Servo</span>
  </button>
  <button class="nav-btn" onclick="sp('i2c')" id="nav-i2c">
    <span class="nav-ic">🔵</span>I2C
  </button>
  <button class="nav-btn" onclick="sp('spi')" id="nav-spi">
    <span class="nav-ic">🟠</span>SPI
  </button>
  <button class="nav-btn" onclick="sp('uart')" id="nav-uart">
    <span class="nav-ic">🔴</span>UART
  </button>

  <div class="nav-section">Tools</div>
  <button class="nav-btn" onclick="sp('manual')" id="nav-manual">
    <span class="nav-ic">🎛️</span>Manual GPIO
  </button>
  <button class="nav-btn" onclick="sp('pinmap')" id="nav-pinmap">
    <span class="nav-ic">📍</span>40-Pin Map
  </button>
  <button class="nav-btn" onclick="sp('sysinfo')" id="nav-sysinfo">
    <span class="nav-ic">📊</span>System Info
  </button>
  <button class="nav-btn" onclick="sp('logview')" id="nav-logview">
    <span class="nav-ic">📋</span>Log Viewer
  </button>
  <button class="nav-btn" onclick="sp('config')" id="nav-config">
    <span class="nav-ic">⚙️</span>Config
  </button>
  <button class="nav-btn" onclick="sp('backends')" id="nav-backends">
    <span class="nav-ic">🔧</span>Backends
  </button>

  <div class="sb-footer" id="sb-footer">
    <span id="sb-status-dot" style="width:7px;height:7px;border-radius:50%;background:var(--green);flex-shrink:0"></span>
    <span id="sb-status-text">Ready</span>
  </div>
</nav>

<!-- ═══════════════ MAIN ═══════════════ -->
<main class="main">

<!-- ── Dashboard ── -->
<div class="page active" id="page-dashboard">
  <div class="page-header">
    <div class="page-title">Dashboard</div>
    <div class="page-sub">Click any card to run a test, or run all at once</div>
  </div>

  <button class="btn btn-run-all" onclick="run('all','td-out')">
    ▶&nbsp;&nbsp;Run All Tests
  </button>

  <div class="test-grid">
    <div class="test-card" id="tc-gpio"     onclick="run('gpio','td-out')">
      <div class="tc-icon">⚡</div>
      <div class="tc-name">GPIO Loopback</div>
      <div class="tc-desc">3 pairs · HIGH + LOW</div>
    </div>
    <div class="test-card" id="tc-gpio_irq" onclick="run('gpio_irq','td-out')">
      <div class="tc-icon">🔔</div>
      <div class="tc-name">GPIO Interrupt</div>
      <div class="tc-desc">Rising edge detect</div>
    </div>
    <div class="test-card" id="tc-pwm"      onclick="run('pwm','td-out')">
      <div class="tc-icon">💡</div>
      <div class="tc-name">PWM LED Ramp</div>
      <div class="tc-desc">0% → 100% duty cycle</div>
    </div>
    <div class="test-card" id="tc-servo"    onclick="run('servo','td-out')">
      <div class="tc-icon">⚙️</div>
      <div class="tc-name">Servo Sweep</div>
      <div class="tc-desc">0° → 180° → 0°</div>
    </div>
    <div class="test-card" id="tc-i2c"      onclick="run('i2c','td-out')">
      <div class="tc-icon">🔵</div>
      <div class="tc-name">I2C Scan</div>
      <div class="tc-desc">Detect bus devices</div>
    </div>
    <div class="test-card" id="tc-spi"      onclick="run('spi','td-out')">
      <div class="tc-icon">🟠</div>
      <div class="tc-name">SPI Loopback</div>
      <div class="tc-desc">MOSI ↔ MISO bridge</div>
    </div>
    <div class="test-card" id="tc-uart"     onclick="run('uart','td-out')">
      <div class="tc-icon">🔴</div>
      <div class="tc-name">UART Loopback</div>
      <div class="tc-desc">TX ↔ RX bridge</div>
    </div>
    <div class="test-card" id="tc-deps"     onclick="run('deps','td-out')">
      <div class="tc-icon">📦</div>
      <div class="tc-name">Dependencies</div>
      <div class="tc-desc">Check installed libs</div>
    </div>
  </div>

  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('td-out')">Clear</button>
  </div>
  <div class="term" id="td-out"><span style="color:var(--text4)">Click a card above to start a test…</span></div>
</div>

<!-- ── GPIO ── -->
<div class="page" id="page-gpio">
  <div class="page-header">
    <div class="page-title">⚡ GPIO Loopback</div>
    <div class="page-sub">Tests digital HIGH/LOW signals across 3 jumper-wired pairs</div>
  </div>
  <div class="wiring">
    <div class="wiring-title">Required Wiring</div>
    GPIO17 (Pin 11) ↔ GPIO27 (Pin 13)<br>
    GPIO22 (Pin 15) ↔ GPIO23 (Pin 16)<br>
    GPIO24 (Pin 18) ↔ GPIO25 (Pin 22)
  </div>
  <div style="display:flex;gap:10px;margin-bottom:14px">
    <button class="btn btn-primary" onclick="run('gpio','tg-out')">▶ Loopback Test</button>
    <button class="btn" onclick="run('gpio_irq','tg-out')">▶ Interrupt Test</button>
  </div>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('tg-out')">Clear</button>
  </div>
  <div class="term" id="tg-out"></div>
</div>

<!-- ── PWM ── -->
<div class="page" id="page-pwm">
  <div class="page-header">
    <div class="page-title">〜 PWM Tests</div>
    <div class="page-sub">LED brightness ramp and servo position sweep</div>
  </div>
  <div class="wiring">
    <div class="wiring-title">LED Ramp Wiring</div>
    GPIO18 (Pin 12) → 330Ω resistor → LED(+) → LED(−) → GND (Pin 6)
  </div>
  <div class="wiring">
    <div class="wiring-title">Servo Wiring</div>
    Signal → GPIO18 (Pin 12)&nbsp;&nbsp;·&nbsp;&nbsp;VCC → 5V (Pin 2)&nbsp;&nbsp;·&nbsp;&nbsp;GND → Pin 6
  </div>
  <div style="display:flex;gap:10px;margin-bottom:14px">
    <button class="btn btn-primary" onclick="run('pwm','tp-out')">▶ LED Ramp</button>
    <button class="btn" onclick="run('servo','tp-out')">▶ Servo Sweep</button>
  </div>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('tp-out')">Clear</button>
  </div>
  <div class="term" id="tp-out"></div>
</div>

<!-- ── I2C ── -->
<div class="page" id="page-i2c">
  <div class="page-header">
    <div class="page-title">🔵 I2C Scan</div>
    <div class="page-sub">Detect devices on the I2C bus</div>
  </div>
  <div class="wiring">
    <div class="wiring-title">Wiring</div>
    SDA → Pin 3&nbsp;&nbsp;·&nbsp;&nbsp;SCL → Pin 5&nbsp;&nbsp;·&nbsp;&nbsp;3.3V → Pin 1 (for sensors)
  </div>
  <div class="card" style="background:var(--blue-lt);border-color:var(--blue-bd);margin-bottom:14px">
    <div style="font-size:12px;color:var(--blue)">
      💡 Enable I2C: <code style="background:rgba(37,99,235,.1);padding:2px 6px;border-radius:4px">sudo raspi-config → Interface Options → I2C → Yes</code>
    </div>
  </div>
  <div style="display:flex;gap:10px;margin-bottom:14px">
    <button class="btn btn-primary" onclick="run('i2c','ti-out')">▶ Scan Bus</button>
  </div>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('ti-out')">Clear</button>
  </div>
  <div class="term" id="ti-out"></div>
</div>

<!-- ── SPI ── -->
<div class="page" id="page-spi">
  <div class="page-header">
    <div class="page-title">🟠 SPI Loopback</div>
    <div class="page-sub">Echoes 0xDEADBEEF via MOSI↔MISO bridge</div>
  </div>
  <div class="wiring">
    <div class="wiring-title">Wiring</div>
    MOSI (Pin 19) ↔ MISO (Pin 21) — one jumper wire
  </div>
  <div class="card" style="background:var(--yel-lt);border-color:var(--yel-bd);margin-bottom:14px">
    <div style="font-size:12px;color:var(--yellow)">
      ⚠ Enable SPI: <code style="background:rgba(217,119,6,.1);padding:2px 6px;border-radius:4px">sudo raspi-config → Interface Options → SPI → Yes</code>
    </div>
  </div>
  <button class="btn btn-primary" onclick="run('spi','ts-out')" style="margin-bottom:14px">▶ SPI Loopback</button>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('ts-out')">Clear</button>
  </div>
  <div class="term" id="ts-out"></div>
</div>

<!-- ── UART ── -->
<div class="page" id="page-uart">
  <div class="page-header">
    <div class="page-title">🔴 UART Loopback</div>
    <div class="page-sub">Sends a test string and reads it back</div>
  </div>
  <div class="wiring">
    <div class="wiring-title">Wiring</div>
    TX (Pin 8) ↔ RX (Pin 10) — one jumper wire
  </div>
  <div class="card" style="background:var(--red-lt);border-color:var(--red-bd);margin-bottom:14px">
    <div style="font-size:12px;color:var(--red)">
      ⚠ Disable serial console first:<br>
      <code style="background:rgba(220,38,38,.08);padding:2px 6px;border-radius:4px">raspi-config → Interface → Serial Port → login shell: No → port hardware: Yes</code>
    </div>
  </div>
  <button class="btn btn-primary" onclick="run('uart','tu-out')" style="margin-bottom:14px">▶ UART Loopback</button>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Output</div>
    <button class="clr-btn" onclick="clr('tu-out')">Clear</button>
  </div>
  <div class="term" id="tu-out"></div>
</div>

<!-- ── Manual GPIO ── -->
<div class="page" id="page-manual">
  <div class="page-header">
    <div class="page-title">🎛️ Manual GPIO Control</div>
    <div class="page-sub">Set output or read input on any BCM pin directly</div>
  </div>

  <div class="gpio-control-grid">
    <div class="card">
      <div class="card-title">Set Output Pin</div>
      <div style="display:flex;gap:10px;margin-bottom:12px">
        <div class="form-group" style="flex:1">
          <label>BCM Pin</label>
          <input type="number" class="form-input" id="out-pin" min="2" max="27" value="18">
        </div>
        <div class="form-group" style="flex:1">
          <label>Value</label>
          <select class="form-input" id="out-val">
            <option value="1">HIGH (1)</option>
            <option value="0">LOW (0)</option>
          </select>
        </div>
      </div>
      <button class="btn btn-success" onclick="gpioSet()">▶ Set Pin</button>
      <div class="gpio-result" id="out-result"></div>
    </div>

    <div class="card">
      <div class="card-title">Read Input Pin</div>
      <div style="margin-bottom:12px">
        <div class="form-group">
          <label>BCM Pin</label>
          <input type="number" class="form-input" id="in-pin" min="2" max="27" value="17">
        </div>
      </div>
      <button class="btn btn-primary" onclick="gpioRead()">▶ Read Pin</button>
      <div class="gpio-result" id="in-result"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Live Pin Snapshot</div>
    <div style="font-size:12px;color:var(--text3);margin-bottom:10px">
      Reads from kernel sysfs — no pins are reconfigured
    </div>
    <button class="btn btn-primary" onclick="run('snapshot','tsnap-out')" style="margin-bottom:12px">↻ Refresh Snapshot</button>
    <div class="term-header">
      <div class="term-label"><span class="term-dot"></span>Snapshot</div>
      <button class="clr-btn" onclick="clr('tsnap-out')">Clear</button>
    </div>
    <div class="term" id="tsnap-out" style="min-height:120px"></div>
  </div>
</div>

<!-- ── 40-Pin Map ── -->
<div class="page" id="page-pinmap">
  <div class="page-header">
    <div class="page-title">📍 40-Pin GPIO Reference</div>
    <div class="page-sub">Click any pin number or label for details · Pin 1 = top-left (USB ports down)</div>
  </div>
  <div class="card">
    <div class="pin-legend" id="pin-legend"></div>
    <div class="divider"></div>
    <div class="pin-map-wrap">
      <table class="pin-map-table" id="pin-map-table"></table>
    </div>
  </div>
  <div class="pin-detail-panel" id="pin-detail-panel">
    <div id="pin-detail-content"></div>
  </div>
</div>

<!-- ── System Info ── -->
<div class="page" id="page-sysinfo">
  <div class="page-header">
    <div class="page-title">📊 System Info</div>
    <div class="page-sub">Raspberry Pi hardware diagnostics</div>
  </div>
  <div class="stat-grid">
    <div class="stat-card">
      <div class="stat-label">GPIO Backend</div>
      <div class="stat-val sv-blue" id="ss-backend">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Status</div>
      <div class="stat-val sv-green" id="ss-status">Ready</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">GPIO</div>
      <div class="stat-val sv-green" id="ss-gpio">—</div>
    </div>
  </div>
  <button class="btn btn-primary" onclick="run('sysinfo','tsi-out')" style="margin-bottom:14px">↻ Refresh</button>
  <div class="term-header">
    <div class="term-label"><span class="term-dot"></span>Full Output</div>
    <button class="clr-btn" onclick="clr('tsi-out')">Clear</button>
  </div>
  <div class="term" id="tsi-out"></div>
</div>

<!-- ── Log Viewer ── -->
<div class="page" id="page-logview">
  <div class="page-header">
    <div class="page-title">📋 Log Viewer</div>
    <div class="page-sub">rpi_test.log — test result history</div>
  </div>
  <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center">
    <button class="btn btn-primary" onclick="loadLog()">↻ Refresh</button>
    <select class="form-input" id="log-lines" onchange="loadLog()" style="width:120px">
      <option value="30">30 lines</option>
      <option value="60" selected>60 lines</option>
      <option value="120">120 lines</option>
      <option value="500">500 lines</option>
    </select>
  </div>
  <div class="card" style="padding:0">
    <div id="log-box" style="padding:14px;max-height:560px;overflow-y:auto">
      <span style="color:var(--text4)">Click Refresh to load log…</span>
    </div>
  </div>
</div>

<!-- ── Config ── -->
<div class="page" id="page-config">
  <div class="page-header">
    <div class="page-title">⚙️ Configuration</div>
    <div class="page-sub">Saved to rpi_test.json · Applied on next run</div>
  </div>
  <div class="card">
    <div class="cfg-grid" id="cfg-form"></div>
    <div class="divider"></div>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="btn btn-primary" onclick="saveCfg()">💾 Save Config</button>
      <span class="cfg-status" id="cfg-status"></span>
    </div>
  </div>
</div>

<!-- ── Backends ── -->
<div class="page" id="page-backends">
  <div class="page-header">
    <div class="page-title">🔧 GPIO Backend Reference</div>
    <div class="page-sub">Available GPIO libraries and how to use them</div>
  </div>
  <div id="backends-list"></div>
</div>

</main>
</div><!-- .shell -->

<!-- Toast -->
<div class="toast hidden" id="toast"></div>

<script>
// ── Page routing ──────────────────────────────────────────────────────────────
function sp(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'))
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'))
  document.getElementById('page-' + name).classList.add('active')
  const nb = document.getElementById('nav-' + name)
  if (nb) nb.classList.add('active')
  if (name === 'pinmap')   buildPinMap()
  if (name === 'logview')  loadLog()
  if (name === 'config')   loadCfg()
  if (name === 'sysinfo')  { loadStats(); run('sysinfo','tsi-out') }
  if (name === 'backends') buildBackends()
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, cls = '') {
  const t = document.getElementById('toast')
  t.textContent = msg; t.className = 'toast ' + cls
  clearTimeout(t._tid)
  t._tid = setTimeout(() => t.classList.add('hidden'), 3000)
}

// ── Terminal helpers ──────────────────────────────────────────────────────────
function clr(id) { document.getElementById(id).innerHTML = '' }

function appendLine(id, text, cls) {
  const t = document.getElementById(id)
  const s = document.createElement('span')
  s.className = cls || 'normal'
  s.textContent = text + '\n'
  t.appendChild(s)
  t.scrollTop = t.scrollHeight
}

// ── SSE Test runner ───────────────────────────────────────────────────────────
let _es = null
function run(name, termId) {
  if (_es) { _es.close(); _es = null }
  clr(termId)
  setStatus('Running ' + name + '…', true)
  const card = document.getElementById('tc-' + name)
  if (card) {
    card.className = 'test-card running'
    const ob = card.querySelector('.tc-badge'); if (ob) ob.remove()
    const b = document.createElement('div')
    b.className = 'tc-badge running'; b.textContent = 'RUNNING'
    card.appendChild(b)
  }
  const es = new EventSource('/api/test/' + name)
  _es = es
  es.onmessage = e => {
    if (e.data === '[DONE]')    { es.close(); _es = null; setStatus('Ready'); return }
    if (e.data === '[TIMEOUT]') { appendLine(termId, '⚠ Timed out', 'warn'); es.close(); _es = null; setStatus('Ready'); return }
    try {
      const d = JSON.parse(e.data)
      appendLine(termId, d.line, d.cls)
      if (card && (d.cls === 'pass' || d.cls === 'fail')) {
        const ob = card.querySelector('.tc-badge'); if (ob) ob.remove()
        const b = document.createElement('div')
        b.className = 'tc-badge ' + d.cls; b.textContent = d.cls.toUpperCase()
        card.appendChild(b); card.className = 'test-card ' + d.cls
      }
    } catch {}
  }
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) return
    appendLine(termId, '✖ Connection error', 'fail')
    es.close(); _es = null; setStatus('Ready')
  }
}

// ── Status bar ────────────────────────────────────────────────────────────────
function setStatus(msg, running = false) {
  const dot  = document.getElementById('sb-status-dot')
  const text = document.getElementById('sb-status-text')
  text.textContent = msg
  dot.style.background = running ? 'var(--yellow)' : 'var(--green)'
  dot.style.animation  = running ? 'breathe 1s infinite' : 'breathe 2s ease-in-out infinite'
}

// ── Poll status every 3s ──────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json())
    document.getElementById('backend-name').textContent = s.backend
    document.getElementById('ss-backend').textContent = s.backend
    document.getElementById('ss-gpio').textContent = s.has_gpio ? 'Available' : 'Not found'
    if (!s.test_running) setStatus('Ready')
  } catch {}
}
setInterval(pollStatus, 3000); pollStatus()

async function loadStats() {
  try {
    const s = await fetch('/api/status').then(r => r.json())
    document.getElementById('ss-backend').textContent = s.backend
    document.getElementById('ss-gpio').textContent = s.has_gpio ? 'Available' : 'Not found'
    document.getElementById('ss-status').textContent = s.test_running ? 'Running…' : 'Ready'
  } catch {}
}

// ── Manual GPIO ───────────────────────────────────────────────────────────────
async function gpioSet() {
  const pin = document.getElementById('out-pin').value
  const val = document.getElementById('out-val').value
  const res = await fetch('/api/gpio/set', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pin, value: val})
  }).then(r => r.json())
  const el = document.getElementById('out-result')
  el.style.display = 'block'
  el.className = 'gpio-result ' + (res.ok ? 'pass' : 'fail')
  el.textContent = res.ok ? '✔ ' + res.msg : '✖ ' + res.msg
  toast(el.textContent, res.ok ? 'pass' : 'fail')
}

async function gpioRead() {
  const pin = document.getElementById('in-pin').value
  const res = await fetch('/api/gpio/read', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({pin})
  }).then(r => r.json())
  const el = document.getElementById('in-result')
  el.style.display = 'block'
  el.className = 'gpio-result ' + (res.ok ? 'pass' : 'fail')
  el.textContent = res.ok ? `✔ GPIO${pin} = ${res.label} (${res.value})` : '✖ ' + res.msg
  toast(el.textContent, res.ok ? 'pass' : 'fail')
}

// ── Log viewer ────────────────────────────────────────────────────────────────
async function loadLog() {
  const n = document.getElementById('log-lines').value
  const data = await fetch('/api/log?lines=' + n).then(r => r.json())
  const c = document.getElementById('log-box'); c.innerHTML = ''
  if (!data.lines.length) {
    c.innerHTML = '<span style="color:var(--text4)">No log entries yet</span>'
    return
  }
  data.lines.forEach(line => {
    const d = document.createElement('div'); d.className = 'log-line'
    if (line.includes('PASS')) d.classList.add('pass')
    else if (line.includes('FAIL')) d.classList.add('fail')
    else if (line.includes('WARN') || line.includes('warn')) d.classList.add('warn')
    d.textContent = line; c.appendChild(d)
  })
  c.scrollTop = c.scrollHeight
}

// ── Config ────────────────────────────────────────────────────────────────────
async function loadCfg() {
  const cfg = await fetch('/api/config').then(r => r.json())
  const f = document.getElementById('cfg-form'); f.innerHTML = ''
  for (const [k, v] of Object.entries(cfg)) {
    const g = document.createElement('div'); g.className = 'form-group'
    g.innerHTML = `
      <label>${k}</label>
      <input type="text" class="form-input" id="cfg-${k}"
             value='${JSON.stringify(v)}'
             style="font-family:'DM Mono',monospace;font-size:12px">
    `
    f.appendChild(g)
  }
}

async function saveCfg() {
  const keys = ['gpio_pairs','pwm_pin','pwm_freq_hz','servo_pin','uart_device','uart_baud','spi_bus','spi_device','spi_speed_hz','i2c_bus']
  const p = {}
  for (const k of keys) {
    const el = document.getElementById('cfg-' + k); if (!el) continue
    try { p[k] = JSON.parse(el.value) } catch { p[k] = el.value }
  }
  const res = await fetch('/api/config', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(p)
  }).then(r => r.json())
  const st = document.getElementById('cfg-status')
  st.textContent = res.ok ? '✔ Saved successfully' : '✖ Failed'
  st.className = 'cfg-status ' + (res.ok ? 'ok' : '')
  toast(st.textContent, res.ok ? 'pass' : 'fail')
}

// ── 40-Pin Map ─────────────────────────────────────────────────────────────────
let _pins = null
async function buildPinMap() {
  const wrap = document.getElementById('pin-map-table')
  if (wrap.innerHTML) return
  if (!_pins) _pins = (await fetch('/api/pinmap').then(r => r.json())).pins

  // Build legend
  const legWrap = document.getElementById('pin-legend')
  const types = ['PWR33','PWR5','GND','GPIO','I2C','SPI','UART','PWM','ID']
  const tnames = {'PWR33':'3.3V','PWR5':'5V','GND':'GND','GPIO':'GPIO',
                  'I2C':'I2C','SPI':'SPI','UART':'UART','PWM':'PWM','ID':'HAT-ID'}
  types.forEach(t => {
    const span = document.createElement('span')
    span.className = 'legend-item pt-' + t
    span.textContent = '■ ' + tnames[t]
    legWrap.appendChild(span)
  })

  // Build table: rows are pairs (pin1|pin2, pin3|pin4, ...)
  // Columns: desc-left | label-left | num-left | separator | num-right | label-right | desc-right
  for (let i = 0; i < _pins.length; i += 2) {
    const L = _pins[i], R = _pins[i+1]
    const tr = document.createElement('tr')
    tr.innerHTML = `
      <td class="pin-cell-left pin-desc" title="${L.desc}">${L.desc}</td>
      <td class="pin-cell-left">
        <span class="pin-label pt-${L.type}" onclick="pinDetail(${L.phys})" title="${L.desc}">${L.label}</span>
      </td>
      <td>
        <div class="pin-num pn-${L.type}" onclick="pinDetail(${L.phys})" title="Pin ${L.phys}">${L.phys}</div>
      </td>
      <td class="pin-sep">│</td>
      <td>
        <div class="pin-num pn-${R.type}" onclick="pinDetail(${R.phys})" title="Pin ${R.phys}">${R.phys}</div>
      </td>
      <td class="pin-cell-right">
        <span class="pin-label pt-${R.type}" onclick="pinDetail(${R.phys})" title="${R.desc}">${R.label}</span>
      </td>
      <td class="pin-cell-right pin-desc" title="${R.desc}">${R.desc}</td>
    `
    wrap.appendChild(tr)
  }
}

function pinDetail(phys) {
  const pin = _pins.find(p => p.phys === phys); if (!pin) return
  const panel = document.getElementById('pin-detail-panel')
  const cont  = document.getElementById('pin-detail-content')
  panel.className = 'pin-detail-panel visible'
  const bcmStr = pin.bcm !== null ? `BCM ${pin.bcm}` : 'No BCM'
  const code = pin.bcm !== null ? `
    <div class="pd-code">
      GPIO.setup(${pin.bcm}, GPIO.OUT)<br>
      GPIO.output(${pin.bcm}, GPIO.HIGH)<br>
      GPIO.output(${pin.bcm}, GPIO.LOW)<br>
      val = GPIO.input(${pin.bcm})
    </div>` : ''
  cont.innerHTML = `
    <div class="pd-header">
      <span class="pd-badge pt-${pin.type}">${pin.label}</span>
      <span style="font-size:15px;font-weight:600">Pin ${pin.phys}</span>
      <span style="color:var(--text3);font-size:13px">${bcmStr} · ${pin.type}</span>
    </div>
    <div style="font-size:13px;color:var(--text2);margin-bottom:8px">${pin.desc}</div>
    ${code}
  `
  panel.scrollIntoView({ behavior:'smooth', block:'nearest' })
}

// ── Backends ──────────────────────────────────────────────────────────────────
function buildBackends() {
  const list = document.getElementById('backends-list')
  if (list.innerHTML) return
  const backends = [
    { name:'RPi.GPIO', env:'rpigpio',  install:'pip install RPi.GPIO',
      info:'Default for Pi 1–4. Not supported on Pi 5.',
      api:'GPIO.setup(), GPIO.output(), GPIO.PWM()' },
    { name:'pigpio',   env:'pigpio',   install:'pip install pigpio  +  sudo systemctl start pigpiod',
      info:'Hardware-timed PWM, servo pulses, remote GPIO. Best timing accuracy.',
      api:'pi.write(), pi.set_servo_pulsewidth()' },
    { name:'lgpio',    env:'lgpio',    install:'pip install lgpio',
      info:'Modern replacement for RPi.GPIO. Works on Pi 5.',
      api:'lgpio.gpio_write(), lgpio.tx_pwm()' },
    { name:'gpiod',    env:'gpiod',    install:'pip install gpiod  (libgpiod >= 2.x)',
      info:'Kernel character device interface. No root required. Software PWM only.',
      api:"chip.get_line(), line.set_value()" },
    { name:'gpiozero', env:'gpiozero', install:'pip install gpiozero',
      info:'High-level abstraction. Great for quick scripts and prototyping.',
      api:'LED(), Button(), PWMLED(), MCP3008()' },
    { name:'smbus2',   env:'—',        install:'pip install smbus2',
      info:'Pure-Python SMBus/I2C. No GPIO dependency.',
      api:'SMBus(1).read_byte(addr), write_byte_data()' },
    { name:'busio',    env:'—',        install:'pip install adafruit-blinka',
      info:'CircuitPython I2C/SPI/UART abstraction (Blinka). Platform-agnostic.',
      api:'busio.I2C(), busio.SPI(), busio.UART()' },
  ]
  backends.forEach(b => {
    const card = document.createElement('div')
    card.className = 'card'
    card.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
        <div style="font-size:15px;font-weight:700;font-family:'DM Mono',monospace">${b.name}</div>
        ${b.env !== '—' ? `<code style="background:var(--blue-lt);color:var(--blue);border:1px solid var(--blue-bd);padding:2px 8px;border-radius:6px;font-size:11px">RPI_GPIO_BACKEND=${b.env}</code>` : ''}
      </div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:8px">${b.info}</div>
      <div style="display:grid;grid-template-columns:70px 1fr;gap:4px;font-size:12px">
        <span style="color:var(--text3);font-weight:600">Install</span>
        <code style="background:var(--bg3);padding:3px 8px;border-radius:5px;font-family:'DM Mono',monospace;font-size:11px">${b.install}</code>
        <span style="color:var(--text3);font-weight:600">Key API</span>
        <code style="background:var(--bg3);padding:3px 8px;border-radius:5px;font-family:'DM Mono',monospace;font-size:11px">${b.api}</code>
      </div>
    `
    list.appendChild(card)
  })
}
</script>
</body>
</html>"""


@app.route("/")
def index(): return HTML


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = 5000
    if "--port" in sys.argv:
        try: port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError): pass

    print("\n  🍓 RPi Hardware Test — Web UI")
    print("  " + "─" * 37)
    print(f"  Local  :  http://127.0.0.1:{port}")
    try:
        ip = subprocess.check_output("hostname -I", shell=True, text=True).split()[0]
        print(f"  Network:  http://{ip}:{port}")
    except Exception:
        pass
    print(f"  Backend:  {core.GPIO_ADAPTER.name}")
    print("  Stop   :  Ctrl+C\n")

    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
