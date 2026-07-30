#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PIDFILE="logs/engine.pid"
mkdir -p logs

case "${1:-help}" in
  start)
    if [ -f "$PIDFILE" ]; then
      pid=$(cat "$PIDFILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "Engine already running (PID $pid). Use: $0 stop"
        exit 1
      fi
      rm -f "$PIDFILE"
    fi
    echo "Starting Predatory Binance Engine in DAEMON mode..."
    nohup python3 main.py --mode daemon > logs/daemon.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started with PID $(cat "$PIDFILE")"
    echo "Logs: logs/daemon.log"
    ;;

  start-live)
    echo "Starting LIVE engine (real trades!)..."
    nohup python3 main.py --mode daemon --live > logs/daemon.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "LIVE engine started with PID $(cat "$PIDFILE")"
    echo "WARNING: Real trades will be executed!"
    ;;

  stop)
    if [ -f "$PIDFILE" ]; then
      pid=$(cat "$PIDFILE")
      echo "Stopping engine (PID $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 2
      if kill -0 "$pid" 2>/dev/null; then
        echo "Force killing..."
        kill -9 "$pid" 2>/dev/null || true
      fi
      rm -f "$PIDFILE"
      echo "Stopped."
    else
      echo "No PID file found."
    fi
    ;;

  status)
    if [ -f "$PIDFILE" ]; then
      pid=$(cat "$PIDFILE")
      if kill -0 "$pid" 2>/dev/null; then
        echo "Engine RUNNING (PID $pid)"
        echo "Uptime: $(ps -o etime= -p "$pid" | xargs)"
      else
        echo "PID $pid exists but process not running. Stale."
        rm -f "$PIDFILE"
      fi
    else
      echo "Engine NOT running"
    fi
    ;;

  once)
    echo "Running single scan..."
    python3 main.py --mode once
    ;;

  dashboard)
    echo "Starting web dashboard at http://localhost:${DASHBOARD_PORT:-8000}"
    echo "Press Ctrl+C to stop"
    cd "$DIR"
    python3 -m uvicorn dashboard:app --host 0.0.0.0 --port "${DASHBOARD_PORT:-8000}" --reload
    ;;

  logs)
    tail -f logs/daemon.log
    ;;

  *)
    echo "PREDATORY BINANCE ENGINE — LAUNCHER"
    echo ""
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  start         Start daemon (dry-run, no real trades)"
    echo "  start-live    Start daemon with LIVE trades"
    echo "  stop          Stop daemon"
    echo "  status        Check if engine is running"
    echo "  once          Run one scan cycle (for manual click)"
    echo "  dashboard     Start web control panel (requires uvicorn)"
    echo "  logs          Follow daemon logs"
    echo ""
    echo "Example:"
    echo "  $0 start      # Start signal generation"
    echo "  $0 once       # Fire one scan now"
    echo "  $0 stop       # Stop everything"
    ;;
esac
