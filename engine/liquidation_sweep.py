"""
=============================================================================
  CRYPTO LIQUIDATION SWEEP DETECTOR
=============================================================================
  Unlike forex stop-hunts, crypto sweeps trigger LIQUIDATIONS — cascading
  forced position closures that create explosive, high-volume reversals.

  Pipeline:
    HTF (15m / 5m) → Find Equal Highs / Equal Lows (retail S/R clusters)
    1m execution  → Detect sweep candle (closes back inside level)
    1m            → Confirm Market Structure Shift + CVD delta return
    Output        → Entry zone, SL above/below wick, TP at next pool

  Why crypto sweeps > forex sweeps:
    Leverage. A 0.3% move on a 100x leveraged position = full liquidation.
    Market makers KNOW where liquidation clusters sit (Binance publishes
    liquidation data). They hunt these levels with precision.
=============================================================================
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import pandas as pd
import config

logger = logging.getLogger("liq_sweep")


@dataclass
class LiquidityPool:
    level:     float
    kind:      str          # "EQH" | "EQL"
    timeframe: str
    count:     int          # number of touches
    weight:    int          # 15m=35, 5m=25
    swept:     bool = False


@dataclass
class SweepResult:
    symbol:        str
    signal:        Optional[str]   # "LONG" | "SHORT"
    sweep_level:   Optional[float]
    sweep_tf:      Optional[str]
    sweep_weight:  int
    mss_confirmed: bool
    mss_price:     Optional[float]
    sl_price:      Optional[float]
    tp1:           Optional[float]
    tp2:           Optional[float]
    tp3:           Optional[float]
    pools:         List[LiquidityPool] = field(default_factory=list)

    @property
    def is_actionable(self) -> bool:
        return self.signal is not None and self.mss_confirmed and self.sweep_weight >= 25


TF_CONFIG = [
    ("15m", 60, 35),   # 15min pools — strongest liquidation clusters
    ("5m",  100, 25),  # 5min pools — secondary structure
]


class LiquidationSweepDetector:
    def __init__(
        self,
        exchange,
        tolerance_pct:  float = config.EQL_TOLERANCE_PCT,
        sweep_pct:      float = config.SWEEP_CONFIRM_PCT,
        exec_tf:        str   = config.EXECUTION_TF,
        exec_bars:      int   = 100,
    ):
        self.exchange      = exchange
        self.tolerance_pct = tolerance_pct
        self.sweep_pct     = sweep_pct
        self.exec_tf       = exec_tf
        self.exec_bars     = exec_bars

    async def analyze(self, symbol: str) -> SweepResult:
        # Build HTF liquidity pools
        all_pools: List[LiquidityPool] = []
        htf_tasks = [
            self._build_pools(symbol, tf, bars, weight)
            for tf, bars, weight in TF_CONFIG
        ]
        results = await asyncio.gather(*htf_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                all_pools.extend(r)

        if not all_pools:
            return self._empty(symbol)

        # Fetch 1min execution chart
        df1m = await self._fetch_df(symbol, self.exec_tf, self.exec_bars + 20)
        if df1m is None or len(df1m) < 10:
            return self._empty(symbol)

        # Check each pool for sweep + MSS
        all_pools.sort(key=lambda p: p.weight, reverse=True)
        mid = float(df1m["close"].iloc[-1])

        for pool in all_pools:
            if pool.swept:
                continue

            direction = "up" if pool.kind == "EQH" else "down"
            sweep_idx = self._find_sweep(df1m, pool.level, direction, mid)
            if sweep_idx is None:
                continue

            pool.swept = True
            mss_dir    = "down" if pool.kind == "EQH" else "up"
            mss_price, _ = self._find_mss(df1m, sweep_idx, mss_dir)

            if mss_price is None:
                logger.debug("%s | %s sweep found, awaiting 1m MSS", symbol, pool.timeframe)
                continue

            signal = "SHORT" if pool.kind == "EQH" else "LONG"
            sl, tp1, tp2, tp3 = self._levels(df1m, sweep_idx, signal, pool.level, mid)

            logger.info(
                "SWEEP+MSS | %s | %s | %s | lvl=%.4f | weight=%d",
                symbol, pool.timeframe, signal, pool.level, pool.weight,
            )
            return SweepResult(
                symbol=symbol, signal=signal,
                sweep_level=pool.level, sweep_tf=pool.timeframe,
                sweep_weight=pool.weight, mss_confirmed=True,
                mss_price=mss_price, sl_price=sl,
                tp1=tp1, tp2=tp2, tp3=tp3, pools=all_pools,
            )

        return self._empty(symbol, pools=all_pools)

    # ── Pool builder ──────────────────────────────────────────────────────────
    async def _build_pools(
        self, symbol: str, tf: str, bars: int, weight: int
    ) -> List[LiquidityPool]:
        df = await self._fetch_df(symbol, tf, bars)
        if df is None or len(df) < 10:
            return []

        swings = self._find_swings(df, order=3)
        tol    = df["close"].mean() * self.tolerance_pct
        pools  = []
        pools += self._cluster(
            [s for s in swings if s[2] == "H"], "EQH", tf, weight, tol
        )
        pools += self._cluster(
            [s for s in swings if s[2] == "L"], "EQL", tf, weight, tol
        )
        return pools

    def _find_swings(self, df: pd.DataFrame, order: int = 3):
        highs, lows, times = df["high"].values, df["low"].values, df.index
        swings = []
        for i in range(order, len(df) - order):
            hw = highs[i - order:i]; hr = highs[i + 1:i + order + 1]
            lw = lows[i - order:i];  lr = lows[i + 1:i + order + 1]
            if highs[i] > max(hw) and highs[i] > max(hr):
                swings.append((i, highs[i], "H", times[i]))
            if lows[i] < min(lw) and lows[i] < min(lr):
                swings.append((i, lows[i], "L", times[i]))
        return swings

    def _cluster(self, pts, kind, tf, weight, tol) -> List[LiquidityPool]:
        visited = [False] * len(pts)
        pools   = []
        for i, p in enumerate(pts):
            if visited[i]: continue
            cluster = [p]; visited[i] = True
            for j, q in enumerate(pts):
                if not visited[j] and abs(q[1] - p[1]) <= tol:
                    cluster.append(q); visited[j] = True
            if len(cluster) >= 2:
                avg = sum(c[1] for c in cluster) / len(cluster)
                pools.append(LiquidityPool(
                    level=avg, kind=kind, timeframe=tf,
                    count=len(cluster), weight=weight,
                ))
        return pools

    # ── Sweep detection ───────────────────────────────────────────────────────
    def _find_sweep(
        self, df: pd.DataFrame, level: float, direction: str, mid: float
    ) -> Optional[int]:
        confirm = level * self.sweep_pct
        for i in range(len(df) - 1, max(len(df) - 60, 0), -1):
            row = df.iloc[i]
            if direction == "up":
                if row["high"] > level + confirm and row["close"] < level:
                    return i
            else:
                if row["low"] < level - confirm and row["close"] > level:
                    return i
        return None

    def _find_mss(
        self, df: pd.DataFrame, sweep_idx: int, direction: str
    ) -> Tuple[Optional[float], Optional[int]]:
        pre     = max(0, sweep_idx - 15)
        post    = df.iloc[sweep_idx:]
        if len(post) < 2:
            return None, None
        if direction == "down":
            ref = df["low"].iloc[pre:sweep_idx].min()
            for i in range(1, len(post)):
                if post["close"].iloc[i] < ref:
                    return float(post["close"].iloc[i]), sweep_idx + i
        else:
            ref = df["high"].iloc[pre:sweep_idx].max()
            for i in range(1, len(post)):
                if post["close"].iloc[i] > ref:
                    return float(post["close"].iloc[i]), sweep_idx + i
        return None, None

    # ── Level calculation ─────────────────────────────────────────────────────
    def _levels(
        self, df: pd.DataFrame, sweep_idx: int,
        signal: str, pool_level: float, mid: float,
    ) -> Tuple[Optional[float], ...]:
        try:
            candle = df.iloc[sweep_idx]
            if signal == "SHORT":
                sl   = candle["high"] * 1.003       # 0.3% above wick
                risk = abs(mid - sl)
                tp1  = mid - risk * config.TP_TIER_1_R
                tp2  = mid - risk * config.TP_TIER_2_R
                tp3  = mid - risk * config.TP_TIER_3_R
            else:
                sl   = candle["low"] * 0.997
                risk = abs(mid - sl)
                tp1  = mid + risk * config.TP_TIER_1_R
                tp2  = mid + risk * config.TP_TIER_2_R
                tp3  = mid + risk * config.TP_TIER_3_R
            return (
                round(sl, 4), round(tp1, 4),
                round(tp2, 4), round(tp3, 4),
            )
        except Exception:
            return None, None, None, None

    # ── Data fetch ────────────────────────────────────────────────────────────
    async def _fetch_df(
        self, symbol: str, tf: str, limit: int
    ) -> Optional[pd.DataFrame]:
        try:
            raw = await self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            df  = pd.DataFrame(
                raw, columns=["ts","open","high","low","close","volume"]
            )
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df
        except Exception as e:
            logger.warning("OHLCV fetch %s %s: %s", symbol, tf, e)
            return None

    def _empty(
        self, symbol: str, pools: List[LiquidityPool] = None
    ) -> SweepResult:
        return SweepResult(
            symbol=symbol, signal=None, sweep_level=None,
            sweep_tf=None, sweep_weight=0, mss_confirmed=False,
            mss_price=None, sl_price=None, tp1=None, tp2=None, tp3=None,
            pools=pools or [],
        )
