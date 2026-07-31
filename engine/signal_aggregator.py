from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
import pandas as pd
from . import ta
import config

logger = logging.getLogger("aggregator")

class KillSwitch:
    def __init__(self, balance: float = config.ACCOUNT_BALANCE_USDT):
        self.balance   = balance
        self.limit     = config.DAILY_DRAWDOWN_LIMIT
        self._daily_pnl = 0.0
        self._tripped  = False
        self._lock     = asyncio.Lock()
        self._date     = datetime.now(timezone.utc).date()

    async def record_pnl(self, pnl: float):
        async with self._lock:
            self._roll()
            self._daily_pnl += pnl
            if -self._daily_pnl / self.balance >= self.limit and not self._tripped:
                self._tripped = True
                logger.critical("KILL SWITCH TRIPPED — daily DD %.2f%%",
                                -self._daily_pnl / self.balance * 100)

    async def can_trade(self) -> bool:
        async with self._lock:
            self._roll()
            return not self._tripped

    def _roll(self):
        today = datetime.now(timezone.utc).date()
        if today != self._date:
            self._daily_pnl = 0.0
            self._tripped   = False
            self._date      = today

    def status(self) -> dict:
        return {"tripped": self._tripped, "daily_pnl": round(self._daily_pnl, 2),
                "drawdown_pct": round(-self._daily_pnl / self.balance * 100, 3)}


@dataclass
class SizingResult:
    setup:         str
    win_prob:      float
    wl_ratio:      float
    raw_kelly:     float
    frac_kelly:    float
    risk_pct:      float
    risk_usdt:     float
    position_usdt: float
    leverage:      int
    atr:           float
    atr_scale:     float


class KellySizer:
    def __init__(self, exchange, profit_locked: bool = False):
        self.exchange     = exchange
        self.profit_locked = profit_locked

    async def compute(
        self, symbol: str, setup: str, sl_pct: float
    ) -> SizingResult:
        p   = config.SETUP_WIN_RATES.get(setup, 0.55)
        b   = config.SETUP_WIN_LOSS_RATIOS.get(setup, 1.5)
        q   = 1 - p
        raw = max(0.0, (p * b - q) / b)
        frac = raw * config.KELLY_FRACTION

        atr, scale = await self._atr_scale(symbol)
        risk_pct   = max(
            config.KELLY_MIN_RISK_PCT,
            min(config.KELLY_MAX_RISK_PCT, frac * scale * 100),
        )
        if self.profit_locked:
            risk_pct /= 2

        risk_usdt     = config.ACCOUNT_BALANCE_USDT * risk_pct / 100
        leverage      = config.LEVERAGE_MAP.get(symbol, 5)
        position_usdt = (risk_usdt / sl_pct) if sl_pct > 0 else 0

        return SizingResult(
            setup=setup, win_prob=p, wl_ratio=b,
            raw_kelly=round(raw, 4), frac_kelly=round(frac, 4),
            risk_pct=round(risk_pct, 3), risk_usdt=round(risk_usdt, 2),
            position_usdt=round(position_usdt, 2), leverage=leverage,
            atr=round(atr, 6), atr_scale=round(scale, 3),
        )

    async def _atr_scale(
        self, symbol: str, tf: str = "1h", period: int = 14, lb: int = 30
    ) -> tuple[float, float]:
        try:
            raw  = await self.exchange.fetch_ohlcv(symbol, tf, limit=lb + period + 5)
            df   = pd.DataFrame(raw, columns=["ts","open","high","low","close","vol"])
            atr  = ta.atr(df["high"], df["low"], df["close"], length=period)
            atr.dropna(inplace=True)
            cur  = float(atr.iloc[-1])
            avg  = float(atr.tail(lb).mean())
            return cur, min(1.0, avg / cur) if cur > 0 else (cur, 1.0)
        except Exception:
            return 0.001, 1.0


STATE_FILE = Path("logs/signal_state.json")

class DailyStateLocker:
    def __init__(self):
        self._state = self._load()

    def can_signal(self) -> bool:
        self._roll_day()
        if self._state.get("today_count", 0) >= config.MAX_SIGNALS_PER_DAY:
            logger.info("STATE LOCKER — daily max (%d) reached.", config.MAX_SIGNALS_PER_DAY)
            return False
        last_str = self._state.get("last_signal_iso")
        if last_str:
            try:
                last = datetime.fromisoformat(last_str)
                elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
                if elapsed < config.SIGNAL_COOLDOWN_HOURS:
                    rem = config.SIGNAL_COOLDOWN_HOURS - elapsed
                    logger.info("STATE LOCKER — cooldown %.1fh remaining.", rem)
                    return False
            except Exception:
                pass
        return True

    def can_rotation(self) -> bool:
        self._roll_day()
        return self._state.get("today_count", 0) < config.MAX_SIGNALS_PER_DAY

    def record(self, symbol: str):
        now = datetime.now(timezone.utc)
        self._state["last_signal_iso"] = now.isoformat()
        self._state["last_signal_sym"] = symbol
        self._state["today_count"]     = self._state.get("today_count", 0) + 1
        self._state["trade_date"]      = str(now.date())
        self._save()
        logger.info("STATE LOCKER — signal #%d recorded for %s. Next in %dh.",
                    self._state["today_count"], symbol, config.SIGNAL_COOLDOWN_HOURS)

    def status(self) -> dict:
        return dict(self._state)

    def _roll_day(self):
        today = str(datetime.now(timezone.utc).date())
        if self._state.get("trade_date") != today:
            self._state = {"trade_date": today, "today_count": 0}
            self._save()

    def _load(self) -> dict:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            logger.warning("State save error: %s", e)


@dataclass
class CompositeSignal:
    symbol:      str
    direction:   str
    score:       int
    label:       str
    entry_price: float
    sl_price:    Optional[float]
    tp1:         Optional[float]
    tp2:         Optional[float]
    tp3:         Optional[float]
    rr_ratio:    float
    leverage:    int
    sweep:       Any = None
    zscore:      Any = None
    obi:         Any = None
    cvd:         Any = None
    funding:     Any = None
    trend:       Any = None
    sizing:      Any = None
    breakdown:   dict = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.score >= config.MIN_SCORE_ACTIONABLE

    @property
    def is_sniper(self) -> bool:
        return self.score >= config.MIN_SCORE_SNIPER


class SignalAggregator:
    def __init__(self):
        self.locker = DailyStateLocker()

    def evaluate(
        self, symbol: str, sweep, zscore, obi, cvd, funding, mid: float, trend=None
    ) -> Optional[CompositeSignal]:

        direction = self._direction(sweep, zscore, obi, cvd, funding, trend)
        if not direction:
            return None

        bd  = {}
        tot = 0

        sw_pts = self._score_sweep(sweep, direction)
        bd["sweep"] = sw_pts; tot += sw_pts

        zs_pts = self._score_z(zscore, direction)
        bd["zscore"] = zs_pts; tot += zs_pts

        oc_pts = self._score_obi_cvd(obi, cvd, direction)
        bd["obi_cvd"] = oc_pts; tot += oc_pts

        fr_pts = self._score_funding(funding, direction)
        bd["funding"] = fr_pts; tot += fr_pts

        tr_pts = self._score_trend(trend, direction)
        bd["trend"] = tr_pts; tot += tr_pts

        label = (
            "SNIPER SHOT"            if tot >= config.MIN_SCORE_SNIPER else
            "HIGH PROBABILITY"       if tot >= 65 else
            "SETUP"                  if tot >= config.MIN_SCORE_ACTIONABLE else
            "NOISE"
        )

        sl = sweep.sl_price if sweep and sweep.is_actionable else None
        rr = self._rr(mid, sl, sweep.tp2 if sweep and sweep.is_actionable else None)

        sig = CompositeSignal(
            symbol=symbol, direction=direction,
            score=tot, label=label,
            entry_price=mid,
            sl_price=sl,
            tp1=sweep.tp1 if sweep and sweep.is_actionable else None,
            tp2=sweep.tp2 if sweep and sweep.is_actionable else None,
            tp3=sweep.tp3 if sweep and sweep.is_actionable else None,
            rr_ratio=rr,
            leverage=config.LEVERAGE_MAP.get(symbol, 5),
            sweep=sweep, zscore=zscore, obi=obi, cvd=cvd,
            funding=funding, trend=trend, breakdown=bd,
        )

        if not sig.is_actionable:
            return sig

        if not self.locker.can_signal():
            return None

        logger.info("COMPOSITE | %s | %s | score=%d | %s",
                    symbol, direction, tot, label)
        return sig

    def confirm_sent(self, symbol: str):
        self.locker.record(symbol)

    def _direction(self, sweep, zscore, obi, cvd, funding, trend=None) -> Optional[str]:
        votes = {"LONG": 0, "SHORT": 0}
        if sweep and sweep.is_actionable:
            w = 3 if sweep.sweep_weight >= 35 else 2
            votes[sweep.signal] += w
        if zscore and zscore.is_entry_signal:
            d = "LONG" if zscore.signal == "LONG" else "SHORT"
            votes[d] += 2
        if obi and obi.is_actionable:
            votes[obi.signal] += 1
        if cvd and cvd.spike and cvd.direction:
            votes[cvd.direction] += 1
        if funding and funding.bias != "NEUTRAL":
            votes["LONG" if funding.bias == "BULLISH" else "SHORT"] += 1
        if trend and trend.is_tradeable:
            w = 2 if trend.strength == "STRONG" else 1
            if trend.trend == "BULL":
                votes["LONG"] += w
            elif trend.trend == "BEAR":
                votes["SHORT"] += w

        if votes["LONG"] >= 2 and votes["LONG"] > votes["SHORT"]:
            return "LONG"
        if votes["SHORT"] >= 2 and votes["SHORT"] > votes["LONG"]:
            return "SHORT"
        if votes["LONG"] > 0 and votes["LONG"] > votes["SHORT"]:
            return "LONG" if votes["LONG"] >= 1 else None
        if votes["SHORT"] > 0 and votes["SHORT"] > votes["LONG"]:
            return "SHORT" if votes["SHORT"] >= 1 else None
        return None

    def _score_sweep(self, sweep, direction) -> int:
        if not sweep or not sweep.is_actionable or sweep.signal != direction:
            return 0
        return sweep.sweep_weight

    def _score_z(self, z, direction) -> int:
        if not z or not z.is_entry_signal: return 0
        if (direction == "LONG" and z.signal == "LONG") or \
           (direction == "SHORT" and z.signal == "SHORT"):
            return 25 if abs(z.zscore) >= 3.0 else 20 if abs(z.zscore) >= 2.75 else 15
        return 0

    def _score_obi_cvd(self, obi, cvd, direction) -> int:
        pts = 0
        if obi and obi.is_actionable and obi.signal == direction:
            pts += 10 + (5 if obi.ratio >= 5 else 0)
        if cvd and cvd.spike and cvd.direction == direction:
            pts += 5
        return min(pts, 20)

    def _score_trend(self, trend, direction) -> int:
        if not trend or not trend.is_tradeable:
            return 0
        if direction == "LONG" and trend.trend == "BULL":
            return 20 if trend.strength == "STRONG" else 12
        if direction == "SHORT" and trend.trend == "BEAR":
            return 20 if trend.strength == "STRONG" else 12
        return 0

    def _score_funding(self, funding, direction) -> int:
        if not funding: return 0
        if direction == "LONG" and not funding.long_aligned: return 0
        if direction == "SHORT" and not funding.short_aligned: return 0
        return 15 + (5 if funding.conviction == "HIGH" else 0)

    def _rr(self, entry, sl, tp) -> float:
        if not entry or not sl or not tp: return 0.0
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        return round(reward / risk, 2) if risk > 0 else 0.0
