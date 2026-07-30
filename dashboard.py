from __future__ import annotations
import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import config

logger = logging.getLogger("dashboard")
app = FastAPI(title="Predatory Binance Engine Dashboard")

PID_FILE = Path("logs/engine.pid")

HTML_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Predatory Binance Engine</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Segoe UI', -apple-system, sans-serif;
      background: #0d1117;
      color: #c9d1d9;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .container {
      max-width: 800px;
      width: 100%;
      padding: 2rem;
    }
    h1 {
      font-size: 1.8rem;
      font-weight: 700;
      margin-bottom: 0.5rem;
      letter-spacing: -0.02em;
    }
    .subtitle {
      color: #8b949e;
      margin-bottom: 2rem;
      font-size: 0.9rem;
    }
    .status-card {
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 1.5rem;
      margin-bottom: 1.5rem;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 1rem;
      margin-top: 1rem;
    }
    .stat-item {
      text-align: center;
    }
    .stat-value {
      font-size: 1.5rem;
      font-weight: 700;
    }
    .stat-label {
      font-size: 0.75rem;
      color: #8b949e;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .running { color: #3fb950; }
    .stopped { color: #f85149; }
    .idle { color: #d29922; }
    .btn-group { display: flex; gap: 0.75rem; flex-wrap: wrap; }
    .btn {
      padding: 0.75rem 1.5rem;
      border: none;
      border-radius: 6px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      flex: 1;
      min-width: 120px;
    }
    .btn:hover { transform: translateY(-1px); }
    .btn-start {
      background: #238636;
      color: #fff;
    }
    .btn-start:hover { background: #2ea043; }
    .btn-start:disabled { background: #3fb950; opacity: 0.5; cursor: not-allowed; }
    .btn-stop {
      background: #da3633;
      color: #fff;
    }
    .btn-stop:hover { background: #f85149; }
    .btn-stop:disabled { opacity: 0.5; cursor: not-allowed; }
    .btn-scan {
      background: #1f6feb;
      color: #fff;
    }
    .btn-scan:hover { background: #388bfd; }
    .btn-scan:disabled { opacity: 0.5; cursor: not-allowed; }
    .log-card {
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 1rem;
      margin-top: 1.5rem;
    }
    .log-card h3 { margin-bottom: 0.75rem; font-size: 0.9rem; color: #8b949e; }
    .log-box {
      background: #0d1117;
      border: 1px solid #21262d;
      border-radius: 4px;
      padding: 0.75rem;
      height: 200px;
      overflow-y: auto;
      font-family: 'Consolas', 'Courier New', monospace;
      font-size: 0.8rem;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    .log-box .info { color: #58a6ff; }
    .log-box .signal { color: #3fb950; }
    .log-box .warn { color: #d29922; }
    .log-box .error { color: #f85149; }
    .footer {
      margin-top: 2rem;
      text-align: center;
      font-size: 0.75rem;
      color: #30363d;
    }
    .badge {
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 12px;
      font-size: 0.7rem;
      font-weight: 600;
    }
    .badge-running { background: #238636; color: #fff; }
    .badge-stopped { background: #da3633; color: #fff; }
  </style>
</head>
<body>
  <div class="container">
    <h1>Predatory Binance Engine</h1>
    <p class="subtitle">8 signals/day · 2h cadence · 5:00–21:00 UTC</p>

    <div class="status-card">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h2>Engine Status</h2>
        <span id="badge" class="badge badge-stopped">OFFLINE</span>
      </div>
      <div class="status-grid">
        <div class="stat-item">
          <div class="stat-value" id="signals-today">0</div>
          <div class="stat-label">Signals Today</div>
        </div>
        <div class="stat-item">
          <div class="stat-value" id="max-signals">8</div>
          <div class="stat-label">Daily Limit</div>
        </div>
        <div class="stat-item">
          <div class="stat-value" id="next-scan">--:--</div>
          <div class="stat-label">Next Scan</div>
        </div>
        <div class="stat-item">
          <div class="stat-value" id="trading-hours">5:00–21:00</div>
          <div class="stat-label">Trading Hours UTC</div>
        </div>
      </div>
      <div style="margin-top:1.5rem;" class="btn-group">
        <button class="btn btn-start" id="btnStart" onclick="startEngine()">Start Engine</button>
        <button class="btn btn-stop" id="btnStop" onclick="stopEngine()" disabled>Stop Engine</button>
        <button class="btn btn-scan" id="btnScan" onclick="scanOnce()">Scan Once</button>
      </div>
    </div>

    <div class="log-card">
      <h3>Engine Log</h3>
      <div class="log-box" id="logBox">
        <span class="info">Dashboard ready. Use "Start Engine" to begin signal generation.</span>
      </div>
    </div>

    <div class="footer">
      Predatory Binance Futures Engine &mdash; v2.0 &mdash; Dry-run mode
    </div>
  </div>

  <script>
    let logCount = 0;

    function addLog(msg, cls = 'info') {
      const box = document.getElementById('logBox');
      const ts = new Date().toLocaleTimeString();
      const el = document.createElement('div');
      el.innerHTML = `<span class="${cls}">[${ts}] ${msg}</span>`;
      box.appendChild(el);
      box.scrollTop = box.scrollHeight;
    }

    async function fetchStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        const badge = document.getElementById('badge');
        const btnStart = document.getElementById('btnStart');
        const btnStop = document.getElementById('btnStop');
        const btnScan = document.getElementById('btnScan');

        if (d.running) {
          badge.textContent = 'RUNNING';
          badge.className = 'badge badge-running';
          btnStart.disabled = true;
          btnStop.disabled = false;
        } else {
          badge.textContent = 'OFFLINE';
          badge.className = 'badge badge-stopped';
          btnStart.disabled = false;
          btnStop.disabled = true;
        }

        document.getElementById('signals-today').textContent = d.signals_today || '0';
        document.getElementById('max-signals').textContent = d.max_signals || '8';
        document.getElementById('next-scan').textContent = d.next_scan || '--:--';
      } catch(e) {
        // ignore
      }
    }

    async function startEngine() {
      try {
        const r = await fetch('/api/start', { method: 'POST' });
        const d = await r.json();
        addLog(d.message, 'signal');
        await fetchStatus();
      } catch(e) {
        addLog('Failed to start: ' + e.message, 'error');
      }
    }

    async function stopEngine() {
      try {
        const r = await fetch('/api/stop', { method: 'POST' });
        const d = await r.json();
        addLog(d.message, 'warn');
        await fetchStatus();
      } catch(e) {
        addLog('Failed to stop: ' + e.message, 'error');
      }
    }

    async function scanOnce() {
      const btn = document.getElementById('btnScan');
      btn.disabled = true;
      addLog('Running scan cycle...', 'info');
      try {
        const r = await fetch('/api/scan', { method: 'POST' });
        const d = await r.json();
        addLog(d.message, d.success ? 'signal' : 'warn');
        await fetchStatus();
      } catch(e) {
        addLog('Scan failed: ' + e.message, 'error');
      }
      btn.disabled = false;
    }

    setInterval(fetchStatus, 5000);
    fetchStatus();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/status")
async def api_status():
    import json
    running = False
    pid = 0
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text().strip())
            proc = await asyncio.create_subprocess_exec(
                "kill", "-0", str(pid),
                stdout=asyncio.DEVNULL, stderr=asyncio.DEVNULL,
            )
            rc = await proc.wait()
            running = (rc == 0)
    except Exception:
        pass

    signals_today = 0
    state_file = Path("logs/signal_state.json")
    try:
        if state_file.exists():
            state = json.loads(state_file.read_text())
            signals_today = state.get("today_count", 0)
    except Exception:
        pass

    from datetime import datetime as dt
    now = dt.now(timezone.utc)
    slot_m = config.SIGNAL_COOLDOWN_HOURS * 60
    cur_m = now.hour * 60 + now.minute
    idx = cur_m // slot_m
    next_m = (idx + 1) * slot_m
    if next_m >= 1440:
        next_m = 0
    next_scan = f"{next_m // 60:02d}:{next_m % 60:02d}"

    return {
        "running": running,
        "pid": pid,
        "signals_today": signals_today,
        "max_signals": config.MAX_SIGNALS_PER_DAY,
        "next_scan": next_scan,
        "cooldown_hours": config.SIGNAL_COOLDOWN_HOURS,
    }


class StartStopResponse(BaseModel):
    success: bool
    message: str


@app.post("/api/start", response_model=StartStopResponse)
async def api_start():
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "run.sh", "start",
            stdout=asyncio.PIPE, stderr=asyncio.PIPE,
            cwd=str(Path(".").resolve()),
        )
        stdout, stderr = await proc.communicate()
        msg = stdout.decode().strip() or "Engine started"
        return StartStopResponse(success=True, message=msg)
    except Exception as e:
        return StartStopResponse(success=False, message=str(e))


@app.post("/api/stop", response_model=StartStopResponse)
async def api_stop():
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "run.sh", "stop",
            stdout=asyncio.PIPE, stderr=asyncio.PIPE,
            cwd=str(Path(".").resolve()),
        )
        stdout, stderr = await proc.communicate()
        msg = stdout.decode().strip() or "Engine stopped"
        return StartStopResponse(success=True, message=msg)
    except Exception as e:
        return StartStopResponse(success=False, message=str(e))


@app.post("/api/scan", response_model=StartStopResponse)
async def api_scan():
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "main.py", "--mode", "once",
            stdout=asyncio.PIPE, stderr=asyncio.PIPE,
            cwd=str(Path(".").resolve()),
        )
        stdout, stderr = await proc.communicate(timeout=60)
        output = stdout.decode().strip()
        if "NOISE" in output or "No signal" in output:
            msg = "Scan complete — no actionable signal found."
            success = False
        elif "signal" in output.lower() or "SNIPER" in output or "SETUP" in output:
            msg = "Signal found! Check logs for details."
            success = True
        else:
            msg = output.split("\n")[-1] if output else "Scan complete."
            success = True
        return StartStopResponse(success=success, message=msg)
    except asyncio.TimeoutError:
        return StartStopResponse(success=False, message="Scan timed out after 60s")
    except Exception as e:
        return StartStopResponse(success=False, message=str(e))
