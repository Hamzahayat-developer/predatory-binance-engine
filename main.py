from __future__ import annotations
import argparse
import asyncio
import logging
import json
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

Path("logs").mkdir(exist_ok=True)

import ccxt.async_support as ccxt

import config
from engine.order_book        import OBIAnalyzer, CVDCalculator
from engine.liquidation_sweep import LiquidationSweepDetector
from engine.stat_arb          import StatArbAnalyzer
from engine.funding_oi        import FundingOIAnalyzer
from engine.signal_aggregator import SignalAggregator, KillSwitch, KellySizer
from engine.trend_filter      import TrendFilter

from notifications.notifier   import DiscordNotifier

logging.basicConfig(
    level   = getattr(logging, config.LOG_LEVEL, logging.INFO),
    format  = "%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

shutdown_event = asyncio.Event()

def _handle_sig(signum, frame):
    logger.warning("Received signal %d — shutting down gracefully...", signum)
    shutdown_event.set()

class PredatoryBinanceEngine:
    def __init__(self, dry_run: bool = True):
        self.dry_run  = dry_run
        self.exchange = ccxt.binanceusdm({
            "apiKey":          config.API_KEY,
            "secret":          config.API_SECRET,
            "enableRateLimit": True,
        })

        self.ks         = KillSwitch()
        self.obi        = OBIAnalyzer()
        self.cvd        = CVDCalculator()
        self.sweep      = LiquidationSweepDetector(self.exchange)
        self.stat_arb   = StatArbAnalyzer(self.exchange)
        self.funding_oi = FundingOIAnalyzer()
        self.trend      = TrendFilter(self.exchange)
        self.aggregator = SignalAggregator()
        self.discord    = DiscordNotifier()

    def _within_trading_hours(self) -> bool:
        now = datetime.now(timezone.utc)
        hour = now.hour
        # UTC hours — for a specific timezone, adjust config
        start = config.TRADING_START_HOUR
        end   = config.TRADING_END_HOUR
        if start <= end:
            return start <= hour < end
        else:
            return hour >= start or hour < end

    def _seconds_until_next_slot(self) -> float:
        now = datetime.now(timezone.utc)
        slot_minutes = config.SIGNAL_COOLDOWN_HOURS * 60
        current_minute = now.hour * 60 + now.minute
        slot_index = current_minute // slot_minutes
        next_slot_minute = (slot_index + 1) * slot_minutes
        next_slot = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_slot_minute)
        diff = (next_slot - now).total_seconds()
        return max(diff, 0)

    def _next_slot_time(self) -> datetime:
        now = datetime.now(timezone.utc)
        slot_m = config.SIGNAL_COOLDOWN_HOURS * 60
        cur_m = now.hour * 60 + now.minute
        idx = cur_m // slot_m
        next_m = (idx + 1) * slot_m
        if next_m >= 24 * 60:
            next_m = 0  # roll to next day
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=next_m)

    async def run_once(self):
        logger.info("=" * 60)
        logger.info("  PREDATORY BINANCE ENGINE — SINGLE SCAN")
        logger.info("  %d pairs · target: 1 signal", len(config.WATCHLIST))
        logger.info("=" * 60)

        if not await self.ks.can_trade():
            logger.critical("Kill switch active")
            await self.discord.send_text("Kill switch active — no signals.")
            return

        signals_found = []
        for symbol in config.WATCHLIST:
            try:
                sig = await self._analyze(symbol)
                if sig and sig.is_actionable:
                    signals_found.append(sig)
                    break
            except Exception as e:
                logger.error("Error analyzing %s: %s", symbol, e, exc_info=True)

        await self._handle_signals(signals_found)
        await self.exchange.close()

    async def run_daemon(self):
        logger.info("=" * 60)
        logger.info("  PREDATORY BINANCE ENGINE — DAEMON MODE")
        logger.info("  Cadence: every %dh · Hours: %d:00-%d:00 UTC",
                     config.SIGNAL_COOLDOWN_HOURS,
                     config.TRADING_START_HOUR, config.TRADING_END_HOUR)
        logger.info("  Max signals/day: %d · Dry-run: %s",
                     config.MAX_SIGNALS_PER_DAY, self.dry_run)
        logger.info("  PID: %d", _get_pid())
        logger.info("=" * 60)

        await self.discord.send_text(
            f"Engine DAEMON started — {config.SIGNAL_COOLDOWN_HOURS}h cadence, "
            f"{config.TRADING_START_HOUR}:00-{config.TRADING_END_HOUR}:00 UTC, "
            f"max {config.MAX_SIGNALS_PER_DAY} signals/day"
        )

        while not shutdown_event.is_set():
            if not self._within_trading_hours():
                now = datetime.now(timezone.utc)
                logger.info("Outside trading hours (%d:00-%d:00 UTC). Sleeping 30min...",
                            config.TRADING_START_HOUR, config.TRADING_END_HOUR)
                try:
                    await asyncio.wait_for(
                        self._sleep_until_trading_start(), timeout=None
                    )
                except asyncio.CancelledError:
                    break
                continue

            await self._scan_and_fire()

            wait_secs = self._seconds_until_next_slot()
            logger.info("Next scan in %.0f minutes (%.0f seconds)...",
                        wait_secs / 60, wait_secs)
            try:
                await asyncio.wait_for(
                    self._sleep_check_shutdown(wait_secs), timeout=wait_secs + 5
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

        logger.info("Daemon shutdown complete.")

    async def _sleep_until_trading_start(self):
        while not shutdown_event.is_set():
            if self._within_trading_hours():
                return
            await asyncio.sleep(30)

    async def _sleep_check_shutdown(self, seconds: float):
        slept = 0
        while slept < seconds and not shutdown_event.is_set():
            await asyncio.sleep(min(5, seconds - slept))
            slept += 5

    async def _scan_and_fire(self):
        if not await self.ks.can_trade():
            logger.critical("Kill switch active")
            await self.discord.send_text("Kill switch active — daemon paused.")
            return

        signals_found = []
        for symbol in config.WATCHLIST:
            try:
                sig = await self._analyze(symbol)
                if sig and sig.is_actionable:
                    signals_found.append(sig)
                    break
            except Exception as e:
                logger.error("Error analyzing %s: %s", symbol, e, exc_info=True)

        await self._handle_signals(signals_found)

    async def _handle_signals(self, signals_found):
        if not signals_found:
            state = self.aggregator.locker.status()
            count = state.get("today_count", 0)
            logger.info("No high-confidence signal. Today: %d/%d.",
                        count, config.MAX_SIGNALS_PER_DAY)

            if count < config.MAX_SIGNALS_PER_DAY:
                fallback = await self._fallback_signal()
                if fallback:
                    signals_found = [fallback]

        if not signals_found:
            state = self.aggregator.locker.status()
            count = state.get("today_count", 0)
            msg = (f"Scan complete — no signal. "
                   f"Today: {count}/{config.MAX_SIGNALS_PER_DAY}")
            logger.info(msg)
            if count == 0:
                await self.discord.send_text(msg)
            return

        for sig in signals_found:
            sl_pct = abs(sig.entry_price - (sig.sl_price or sig.entry_price)) / sig.entry_price
            sizer  = KellySizer(self.exchange)
            sizing = await sizer.compute(
                sig.symbol,
                sig.label.split("|")[-1].strip() if "|" in sig.label else "COMPOSITE",
                sl_pct,
            )
            sig.sizing = sizing

            state = self.aggregator.locker.status()
            await self.discord.send_signal(sig, sizing, state)
            self.aggregator.confirm_sent(sig.symbol)
            self._log_signal(sig, sizing)
            self._save_signal_json(sig)

    async def _fallback_signal(self):
        logger.info("Attempting fallback rotation signal...")
        pair, bias, reason = await self._find_best_rotation()
        if not pair:
            return None

        mid = 0.0
        try:
            ob = await self._safe_fetch_ob(pair)
            if ob and "bids" in ob and "asks" in ob:
                bids = ob.get("bids", [])
                asks = ob.get("asks", [])
                mid = ((bids[0][0] if bids else 0) + (asks[0][0] if asks else 0)) / 2
        except Exception:
            pass

        if mid == 0.0:
            return None

        sl_pct = 0.003
        risk = mid * sl_pct
        sl = mid - risk if bias == "LONG" else mid + risk
        tp1 = mid + risk * 1.0 if bias == "LONG" else mid - risk * 1.0
        tp2 = mid + risk * 2.0 if bias == "LONG" else mid - risk * 2.0
        tp3 = mid + risk * 3.5 if bias == "LONG" else mid - risk * 3.5

        from engine.signal_aggregator import CompositeSignal
        sig = CompositeSignal(
            symbol=pair, direction=bias,
            score=config.MIN_SCORE_FALLBACK,
            label=f"ROTATION | {pair} | {reason}",
            entry_price=mid, sl_price=round(sl, 4),
            tp1=round(tp1, 4), tp2=round(tp2, 4), tp3=round(tp3, 4),
            rr_ratio=round(risk / risk * 3.5, 2),
            leverage=config.LEVERAGE_MAP.get(pair, 5),
            breakdown={"rotation": config.MIN_SCORE_FALLBACK},
        )
        return sig

    async def _find_best_rotation(self):
        candidates = []
        for symbol in config.WATCHLIST:
            try:
                trend_r  = await self.trend.analyze(symbol)
                ohlcv_1m = await self._safe_ohlcv(symbol, "1m", 30)
                mid = 0.0
                if ohlcv_1m and len(ohlcv_1m) > 0:
                    mid = float(ohlcv_1m[-1][4])
                fund_r   = await self.funding_oi.analyze(symbol)
                fund_bias = None
                if fund_r and fund_r.bias != "NEUTRAL":
                    fund_bias = "LONG" if fund_r.bias == "BULLISH" else "SHORT"
                candidates.append((symbol, trend_r, fund_bias, mid))
            except Exception:
                continue

        if not candidates:
            if config.WATCHLIST:
                return config.WATCHLIST[0], "LONG", "default rotation"
            return None, None, None

        # Score each candidate: trend alignment + funding bias
        scored = []
        for sym, trend_r, fund_bias, price in candidates:
            score = 0
            bias = "LONG"
            if trend_r and trend_r.is_tradeable:
                if trend_r.trend == "BULL":
                    score += trend_r.regime_score
                    bias = "LONG"
                elif trend_r.trend == "BEAR":
                    score += abs(trend_r.regime_score)
                    bias = "SHORT"
            if fund_bias:
                score += 15
                bias = fund_bias
            if price > 0:
                scored.append((sym, bias, score))

        if not scored:
            return config.WATCHLIST[0], "LONG", "default"

        scored.sort(key=lambda x: x[2], reverse=True)
        sym, bias, score = scored[0]
        reason = f"trend+funding rotation (score={score})"
        logger.info("Rotation | %s | %s | score=%d", sym, bias, score)
        return sym, bias, reason

    async def _analyze(self, symbol: str):
        ob     = await self._safe_fetch_ob(symbol)
        obi_r  = self.obi.analyze(symbol, ob["bids"], ob["asks"]) if ob else None
        mid    = obi_r.mid_price if obi_r else 0.0

        ohlcv_1m = await self._safe_ohlcv(symbol, "1m", 30)
        cvd_r    = self.cvd.calculate(symbol, ohlcv_1m) if ohlcv_1m else None

        sweep_r  = await self.sweep.analyze(symbol)
        z_r      = await self.stat_arb.analyze(symbol)
        fund_r   = await self.funding_oi.analyze(symbol)
        trend_r  = await self.trend.analyze(symbol, mid) if mid > 0 else None

        if trend_r and not self.trend.validate_direction(trend_r, sweep_r.signal if sweep_r and sweep_r.is_actionable else ""):
            return None

        return self.aggregator.evaluate(symbol, sweep_r, z_r, obi_r, cvd_r, fund_r, mid, trend_r)

    async def _safe_fetch_ob(self, symbol: str) -> dict:
        try:
            return await self.exchange.fetch_order_book(symbol, limit=20)
        except Exception as e:
            logger.debug("OB fetch %s: %s", symbol, e)
            return {}

    async def _safe_ohlcv(self, symbol: str, tf: str, limit: int):
        try:
            return await self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
        except Exception as e:
            logger.debug("OHLCV fetch %s %s: %s", symbol, tf, e)
            return None

    def _log_signal(self, sig, sizing):
        logger.info("-" * 60)
        logger.info("  %s  ·  %s  ·  SCORE: %d/100", sig.label, sig.symbol, sig.score)
        logger.info("  Direction  : %s  (%dx Isolated)", sig.direction, sig.leverage)
        logger.info("  Entry      : %.4f", sig.entry_price)
        logger.info("  Stop Loss  : %.4f", sig.sl_price or 0)
        logger.info("  TP1 (1R)   : %.4f  close 40%%", sig.tp1 or 0)
        logger.info("  TP2 (2R)   : %.4f  close 40%%", sig.tp2 or 0)
        logger.info("  TP3 (3.5R) : %.4f  close 20%%", sig.tp3 or 0)
        logger.info("  R:R        : 1:%.2f", sig.rr_ratio)
        logger.info("  Risk       : $%.2f (%.2f%%)", sizing.risk_usdt, sizing.risk_pct)
        logger.info("  Notional   : $%.0f at %dx", sizing.position_usdt, sig.leverage)
        if sig.sweep and sig.sweep.is_actionable:
            logger.info("  Sweep TF   : %s  (weight=%d)", sig.sweep.sweep_tf, sig.sweep.sweep_weight)
        if sig.funding:
            logger.info("  Funding    : %+.4f%%  OI: %+.2f%%",
                        sig.funding.funding_rate * 100, sig.funding.oi_change_pct)
        logger.info("-" * 60)

    def _save_signal_json(self, sig):
        data = {
            "symbol": sig.symbol,
            "direction": sig.direction,
            "score": sig.score,
            "label": sig.label,
            "entryPrice": sig.entry_price,
            "slPrice": sig.sl_price or 0,
            "tp1": sig.tp1 or 0,
            "tp2": sig.tp2 or 0,
            "tp3": sig.tp3 or 0,
            "rrRatio": sig.rr_ratio,
            "leverage": sig.leverage,
            "breakdown": sig.breakdown,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            Path("logs").mkdir(exist_ok=True)
            Path("logs/last_signal.json").write_text(json.dumps(data, indent=2))
            logger.info("Signal saved to logs/last_signal.json")
        except Exception as e:
            logger.warning("Save signal JSON: %s", e)


def _pid_file() -> Path:
    return Path("logs/engine.pid")

def _write_pid():
    _pid_file().write_text(str(_get_pid()))

def _read_pid() -> int:
    try:
        return int(_pid_file().read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0

def _get_pid() -> int:
    try:
        return _get_pid.cached
    except AttributeError:
        _get_pid.cached = 0
        return 0
_get_pid.cached = 0

def parse_args():
    p = argparse.ArgumentParser(description="Predatory Binance Futures Engine")
    p.add_argument("--mode", choices=["once", "daemon", "start", "stop", "status"], default="once")
    p.add_argument("--live", action="store_true", default=False)
    return p.parse_args()


async def main():
    args   = parse_args()
    _get_pid.cached = 0

    if args.mode == "stop":
        pid = _read_pid()
        if pid:
            import os
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"Sent SIGTERM to PID {pid}")
            except ProcessLookupError:
                print(f"PID {pid} not found. Cleaning stale pid file.")
                _pid_file().unlink(missing_ok=True)
        else:
            print("No running engine found.")
        return

    if args.mode == "status":
        pid = _read_pid()
        if pid:
            import os
            try:
                os.kill(pid, 0)
                print(f"Engine running with PID {pid}")
                return
            except ProcessLookupError:
                print(f"Stale PID {pid}. Engine not running.")
                _pid_file().unlink(missing_ok=True)
        else:
            print("Engine not running.")
        return

    if args.mode == "start":
        import subprocess, sys
        cmd = [sys.executable, __file__, "--mode", "daemon"]
        if args.live:
            cmd.append("--live")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _write_pid()
        print(f"Engine started with PID {proc.pid}")
        return

    if args.mode == "daemon":
        _get_pid.cached = 0
        _write_pid()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    engine = PredatoryBinanceEngine(dry_run=not args.live)

    if args.mode == "once":
        await engine.run_once()
    elif args.mode == "daemon":
        await engine.run_daemon()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
