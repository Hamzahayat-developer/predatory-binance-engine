"""
=============================================================================
  STATISTICAL ARBITRAGE — Crypto Basket Z-Score
=============================================================================
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional
import numpy as np
import pandas as pd
import config

logger = logging.getLogger("stat_arb")


@dataclass
class ZScoreResult:
    symbol:        str
    zscore:        float
    spread:        float
    signal:        Optional[str]   # "LONG" | "SHORT" | "EXIT"
    basket:        List[str]
    window:        int

    @property
    def is_entry_signal(self) -> bool:
        return self.signal in ("LONG", "SHORT")


class StatArbAnalyzer:
    def __init__(
        self,
        exchange,
        window:    int   = config.ZSCORE_WINDOW,
        threshold: float = config.ZSCORE_ENTRY,
        exit_thr:  float = config.ZSCORE_EXIT,
        timeframe: str   = config.ZSCORE_TIMEFRAME,
        baskets:   Dict[str, List[str]] = None,
    ):
        self.exchange  = exchange
        self.window    = window
        self.threshold = threshold
        self.exit_thr  = exit_thr
        self.tf        = timeframe
        self.baskets   = baskets or config.BASKETS

    async def analyze(self, symbol: str) -> Optional[ZScoreResult]:
        basket = self.baskets.get(symbol)
        if not basket:
            return None

        all_syms = [symbol] + basket
        tasks    = [self._get_close(s) for s in all_syms]
        series   = await asyncio.gather(*tasks, return_exceptions=True)

        frames = {}
        for sym, res in zip(all_syms, series):
            if not isinstance(res, Exception) and res is not None:
                frames[sym] = res

        if len(frames) < 2:
            return None

        df   = pd.DataFrame(frames).dropna()
        if len(df) < self.window + 5:
            return None

        norm    = df / df.iloc[0] * 100
        primary = norm[symbol]
        bask    = norm[[s for s in basket if s in norm.columns]].mean(axis=1)
        spread  = primary - bask
        z       = self._zscore(spread, self.window)
        zval    = float(z.iloc[-1])
        signal  = self._signal(zval)

        if signal in ("LONG", "SHORT"):
            logger.info("STAT-ARB | %s | Z=%.3fσ | signal=%s", symbol, zval, signal)

        return ZScoreResult(
            symbol=symbol, zscore=round(zval, 4),
            spread=round(float(spread.iloc[-1]), 4),
            signal=signal, basket=basket, window=self.window,
        )

    async def analyze_all(self) -> List[ZScoreResult]:
        tasks = [self.analyze(s) for s in self.baskets]
        res   = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in res if isinstance(r, ZScoreResult)]

    async def _get_close(self, symbol: str) -> Optional[pd.Series]:
        try:
            ohlcv = await self.exchange.fetch_ohlcv(
                symbol, self.tf, limit=self.window + 10
            )
            df = pd.DataFrame(
                ohlcv, columns=["ts","open","high","low","close","volume"]
            )
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df["close"].rename(symbol)
        except Exception as e:
            logger.debug("Close fetch %s: %s", symbol, e)
            return None

    @staticmethod
    def _zscore(s: pd.Series, w: int) -> pd.Series:
        m   = s.rolling(w, min_periods=w).mean()
        std = s.rolling(w, min_periods=w).std(ddof=1)
        return (s - m) / std.replace(0, np.nan)

    def _signal(self, z: float) -> Optional[str]:
        if np.isnan(z): return None
        if z <= -self.threshold: return "LONG"
        if z >= +self.threshold: return "SHORT"
        if abs(z) <= self.exit_thr: return "EXIT"
        return None
