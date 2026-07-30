from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np
import pandas_ta as ta

logger = logging.getLogger("trend_filter")


@dataclass
class TrendResult:
    symbol:        str
    trend:         str          # "BULL" | "BEAR" | "NEUTRAL"
    strength:      str          # "STRONG" | "MODERATE" | "WEAK"
    ema200_price:  float
    ema50_price:   float
    ema9_price:    float
    price_vs_ema200: float     # % distance from 200 EMA
    adx:           float
    volatility:    str          # "HIGH" | "NORMAL" | "LOW"
    volume_regime: str          # "RISING" | "FALLING" | "NEUTRAL"
    regime_score:  int          # -100 to +100
    is_tradeable:  bool


class TrendFilter:
    def __init__(self, exchange):
        self.exchange = exchange

    async def analyze(self, symbol: str, current_price: float = 0.0) -> Optional[TrendResult]:
        try:
            df_1h = await self._fetch_ohlcv(symbol, "1h", 250)
            if df_1h is None or len(df_1h) < 200:
                return None

            close = df_1h["close"].astype(float)
            high  = df_1h["high"].astype(float)
            low   = df_1h["low"].astype(float)

            ema200 = ta.ema(close, length=200)
            ema50  = ta.ema(close, length=50)
            ema9   = ta.ema(close, length=9)
            ema20  = ta.ema(close, length=20)

            adx_df = ta.adx(high, low, close, length=14)

            cur_price    = current_price if current_price > 0 else float(close.iloc[-1])
            cur_ema200   = float(ema200.iloc[-1]) if ema200 is not None and len(ema200) > 0 else cur_price
            cur_ema50    = float(ema50.iloc[-1]) if ema50 is not None and len(ema50) > 0 else cur_price
            cur_ema9     = float(ema9.iloc[-1]) if ema9 is not None and len(ema9) > 0 else cur_price
            cur_ema20    = float(ema20.iloc[-1]) if ema20 is not None and len(ema20) > 0 else cur_price

            price_vs_200 = (cur_price - cur_ema200) / cur_ema200 * 100

            # ADX for trend strength
            adx_val = 25.0
            if adx_df is not None:
                adx_series = adx_df.get(f"ADX_14", None)
                if adx_series is not None and len(adx_series) > 0:
                    adx_val = float(adx_series.iloc[-1])

            # Trend direction
            if cur_price > cur_ema200 and cur_ema9 > cur_ema20:
                trend = "BULL"
            elif cur_price < cur_ema200 and cur_ema9 < cur_ema20:
                trend = "BEAR"
            else:
                trend = "NEUTRAL"

            # Strength
            if adx_val >= 30:
                strength = "STRONG"
            elif adx_val >= 20:
                strength = "MODERATE"
            else:
                strength = "WEAK"

            # Volatility
            atr = ta.atr(high, low, close, length=14)
            atr_pct = 0.0
            if atr is not None and len(atr) > 0:
                atr_pct = float(atr.iloc[-1]) / cur_price * 100
            if atr_pct >= 3.0:
                volatility = "HIGH"
            elif atr_pct >= 1.0:
                volatility = "NORMAL"
            else:
                volatility = "LOW"

            # Volume regime
            vol = df_1h["volume"].astype(float)
            vol_ma20 = vol.rolling(20).mean()
            vol_regime = "NEUTRAL"
            if len(vol_ma20) > 0 and len(vol) > 0:
                cur_vol = float(vol.iloc[-1])
                cur_vol_ma = float(vol_ma20.iloc[-1])
                if cur_vol_ma > 0:
                    vol_ratio = cur_vol / cur_vol_ma
                    if vol_ratio >= 1.5:
                        vol_regime = "RISING"
                    elif vol_ratio <= 0.5:
                        vol_regime = "FALLING"

            # Regime score: -100 to +100
            score = 0
            if trend == "BULL":
                score += 35 if strength == "STRONG" else 20 if strength == "MODERATE" else 10
                score += 15 if vol_regime == "RISING" else 5
            elif trend == "BEAR":
                score -= 35 if strength == "STRONG" else 20 if strength == "MODERATE" else 10
                score -= 15 if vol_regime == "RISING" else 5
            else:
                if cur_ema9 > cur_ema20:
                    score += 10
                elif cur_ema9 < cur_ema20:
                    score -= 10

            # Volatility adjustment
            if volatility == "HIGH":
                score = int(score * 0.7)
            elif volatility == "LOW":
                score = int(score * 1.2)

            is_tradeable = abs(score) >= 15 and strength != "WEAK"

            result = TrendResult(
                symbol=symbol, trend=trend, strength=strength,
                ema200_price=round(cur_ema200, 2), ema50_price=round(cur_ema50, 2),
                ema9_price=round(cur_ema9, 2),
                price_vs_ema200=round(price_vs_200, 2),
                adx=round(adx_val, 1), volatility=volatility,
                volume_regime=vol_regime, regime_score=score,
                is_tradeable=is_tradeable,
            )

            logger.info("TREND | %s | %s %s | ADX=%.1f | score=%+d | tradeable=%s",
                        symbol, trend, strength, adx_val, score, is_tradeable)
            return result

        except Exception as e:
            logger.warning("TrendFilter error %s: %s", symbol, e)
            return None

    async def _fetch_ohlcv(self, symbol: str, tf: str, limit: int):
        try:
            raw = await self.exchange.fetch_ohlcv(symbol, tf, limit=limit)
            if not raw:
                return None
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df.set_index("ts", inplace=True)
            return df
        except Exception as e:
            logger.warning("OHLCV fetch %s %s: %s", symbol, tf, e)
            return None

    def validate_direction(self, trend: Optional[TrendResult], direction: str) -> bool:
        if trend is None:
            return True
        if not trend.is_tradeable:
            return True
        if direction == "LONG" and trend.trend == "BEAR":
            return False
        if direction == "SHORT" and trend.trend == "BULL":
            return False
        return True
