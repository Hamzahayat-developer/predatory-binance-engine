"""Lightweight technical indicators - drop-in replacement for pandas_ta.

Implements only what the engine uses (ema, atr, adx) with pandas/numpy
so we don't depend on the pandas_ta package.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series, length: int):
    return series.astype(float).ewm(span=length, adjust=False).mean()


def sma(series, length: int):
    return series.astype(float).rolling(length).mean()


def _true_range(high, low, close):
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(high, low, close, length: int = 14):
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1.0 / length, adjust=False).mean()


def adx(high, low, close, length: int = 14):
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)

    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=high.index)

    tr = _true_range(high, low, close)
    atr_series = tr.ewm(alpha=1.0 / length, adjust=False).mean()
    atr_denom = atr_series.replace(0, np.nan)

    plus_di = 100 * plus_dm.ewm(alpha=1.0 / length, adjust=False).mean() / atr_denom
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / length, adjust=False).mean() / atr_denom

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_series = dx.ewm(alpha=1.0 / length, adjust=False).mean()

    return pd.DataFrame(
        {
            f"ADX_{length}": adx_series,
            f"DMP_{length}": plus_di,
            f"DMN_{length}": minus_di,
        },
        index=high.index,
    )
