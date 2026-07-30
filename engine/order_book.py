"""
=============================================================================
  ORDER BOOK IMBALANCE (OBI) + CUMULATIVE VOLUME DELTA (CVD)
=============================================================================
  OBI:  Measures directional pressure in top-N price levels of L2 book.
        Ratio ≥ 3:1 = institutional size stacking one side.

  CVD:  Cumulative Volume Delta = sum of (buy_vol - sell_vol) per candle.
        A sharp CVD spike confirms aggressive market orders hitting one side.
        Divergence between price and CVD = hidden institutional absorption.
=============================================================================
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import List, Tuple, Optional
import numpy as np
import config

logger = logging.getLogger("obi_cvd")
OrderLevel = Tuple[float, float]


@dataclass
class OBIResult:
    symbol:          str
    bid_volume:      float
    ask_volume:      float
    imbalance_score: float     # -1 to +1
    ratio:           float
    signal:          Optional[str]   # "BUY" | "SELL" | None
    mid_price:       float
    levels_used:     int

    @property
    def is_actionable(self) -> bool:
        return self.signal is not None


@dataclass
class CVDResult:
    symbol:       str
    cvd_current:  float     # latest bar CVD
    cvd_rolling:  float     # rolling mean
    cvd_std:      float     # rolling std
    cvd_zscore:   float     # how many σ above mean
    spike:        bool      # True if |cvd_zscore| >= threshold
    direction:    Optional[str]   # "BUY" | "SELL" — direction of spike


class OBIAnalyzer:
    def __init__(
        self,
        levels:        int   = config.OBI_LEVELS,
        trigger_ratio: float = config.OBI_TRIGGER_RATIO,
    ):
        self.levels        = levels
        self.trigger_ratio = trigger_ratio

    def analyze(
        self,
        symbol: str,
        bids:   List[OrderLevel],
        asks:   List[OrderLevel],
    ) -> OBIResult:
        n       = min(self.levels, len(bids), len(asks))
        if n == 0:
            return self._empty(symbol, bids, asks)

        bid_vol = sum(v for _, v in bids[:n])
        ask_vol = sum(v for _, v in asks[:n])
        total   = bid_vol + ask_vol
        if total == 0:
            return self._empty(symbol, bids, asks)

        imbalance = (bid_vol - ask_vol) / total
        mid       = ((bids[0][0] if bids else 0) + (asks[0][0] if asks else 0)) / 2

        signal, ratio = self._classify(bid_vol, ask_vol)

        result = OBIResult(
            symbol=symbol, bid_volume=round(bid_vol, 4),
            ask_volume=round(ask_vol, 4), imbalance_score=round(imbalance, 4),
            ratio=round(ratio, 3), signal=signal,
            mid_price=mid, levels_used=n,
        )
        if result.is_actionable:
            logger.info("OBI | %s | %s | ratio=%.2f:1 | imb=%.3f",
                        symbol, signal, ratio, imbalance)
        return result

    def _classify(self, bv: float, av: float) -> Tuple[Optional[str], float]:
        if av == 0: return ("BUY", float("inf")) if bv > 0 else (None, 1.0)
        if bv == 0: return "SELL", float("inf")
        if av / bv >= self.trigger_ratio: return "SELL", round(av / bv, 3)
        if bv / av >= self.trigger_ratio: return "BUY",  round(bv / av, 3)
        return None, max(bv / av, av / bv)

    def _empty(self, symbol, bids, asks) -> OBIResult:
        mid = ((bids[0][0] if bids else 0) + (asks[0][0] if asks else 0)) / 2
        return OBIResult(symbol=symbol, bid_volume=0, ask_volume=0,
                         imbalance_score=0, ratio=1.0, signal=None,
                         mid_price=mid, levels_used=0)


class CVDCalculator:
    """
    Computes Cumulative Volume Delta from OHLCV data.
    CVD per bar ≈ (close > open) ? +volume : -volume
    For a true CVD you'd need tick data — this is the standard approximation
    used across professional trading tools when tick data is unavailable.
    """
    def __init__(self, spike_threshold: float = config.CVD_SPIKE_THRESHOLD):
        self.spike_threshold = spike_threshold

    def calculate(self, symbol: str, ohlcv: list) -> CVDResult:
        if not ohlcv or len(ohlcv) < 5:
            return CVDResult(symbol=symbol, cvd_current=0, cvd_rolling=0,
                             cvd_std=1, cvd_zscore=0, spike=False, direction=None)

        deltas = []
        for bar in ohlcv:
            _, o, h, l, c, v = bar
            # Refined approximation using candle body direction + wick pressure
            body_dir = 1.0 if c >= o else -1.0
            # Volume attribution: bullish/bearish based on close position in range
            hl_range = h - l if (h - l) > 0 else 1e-10
            bull_frac = (c - l) / hl_range
            bear_frac = (h - c) / hl_range
            delta = v * (bull_frac - bear_frac)
            deltas.append(delta)

        arr           = np.array(deltas)
        cvd_current   = float(arr[-1])
        rolling_mean  = float(arr.mean())
        rolling_std   = float(arr.std(ddof=1)) or 1.0
        zscore        = (cvd_current - rolling_mean) / rolling_std
        spike         = abs(zscore) >= self.spike_threshold
        direction     = ("BUY" if zscore > 0 else "SELL") if spike else None

        if spike:
            logger.info("CVD SPIKE | %s | z=%.2fσ | direction=%s",
                        symbol, zscore, direction)

        return CVDResult(
            symbol=symbol, cvd_current=round(cvd_current, 4),
            cvd_rolling=round(rolling_mean, 4), cvd_std=round(rolling_std, 4),
            cvd_zscore=round(zscore, 3), spike=spike, direction=direction,
        )
