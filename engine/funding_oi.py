"""
=============================================================================
  OPEN INTEREST + FUNDING RATE — Crypto Smart Money Sentiment
=============================================================================
  Replaces COT data. Binance Futures publishes OI and Funding Rate publicly.

  RULES:
    LONG signals:
      • Funding rate NEGATIVE or near zero (shorts paying longs = bullish)
      • Open Interest RISING (new money entering market = conviction)
      • OI rising + price rising = genuine uptrend, not short squeeze

    SHORT signals:
      • Funding rate POSITIVE and high (longs paying shorts = bearish)
      • Open Interest RISING (shorts building position)
      • OI rising + price falling = genuine downtrend

  Funding rates update every 8 hours on Binance.
  OI updates in real time.
=============================================================================
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict
import aiohttp
import config

logger = logging.getLogger("funding_oi")

BINANCE_FAPI = "https://fapi.binance.com"


@dataclass
class FundingOIResult:
    symbol:           str
    funding_rate:     float
    oi_current:       float
    oi_prev:          float
    oi_change_pct:    float
    oi_rising:        bool
    bias:             str           # "BULLISH" | "BEARISH" | "NEUTRAL"
    conviction:       str           # "HIGH" | "MEDIUM" | "LOW"
    long_aligned:     bool
    short_aligned:    bool


def _binance_symbol(ccxt_symbol: str) -> str:
    """Convert ccxt symbol to Binance FAPI symbol. BTC/USDT:USDT → BTCUSDT"""
    return ccxt_symbol.split("/")[0] + "USDT"


class FundingOIAnalyzer:
    def __init__(self):
        self._oi_cache: Dict[str, float] = {}

    async def analyze(self, symbol: str) -> Optional[FundingOIResult]:
        bsym = _binance_symbol(symbol)
        try:
            funding, oi = await asyncio.gather(
                self._get_funding(bsym),
                self._get_oi(bsym),
                return_exceptions=True,
            )
            if isinstance(funding, Exception) or isinstance(oi, Exception):
                return None

            # OI change
            prev_oi       = self._oi_cache.get(bsym, oi)
            oi_change_pct = (oi - prev_oi) / prev_oi if prev_oi > 0 else 0
            self._oi_cache[bsym] = oi
            oi_rising     = oi_change_pct >= config.OI_CHANGE_THRESHOLD

            # Classify
            fr            = funding
            long_aligned  = (
                fr <= config.FUNDING_LONG_MAX and oi_rising
            )
            short_aligned = (
                fr >= config.FUNDING_SHORT_MIN and oi_rising
            )

            if fr < 0 and oi_rising:
                bias, conviction = "BULLISH", "HIGH"
            elif fr <= config.FUNDING_LONG_MAX and oi_rising:
                bias, conviction = "BULLISH", "MEDIUM"
            elif fr > 0.0005 and oi_rising:
                bias, conviction = "BEARISH", "HIGH"
            elif fr > 0 and oi_rising:
                bias, conviction = "BEARISH", "MEDIUM"
            else:
                bias, conviction = "NEUTRAL", "LOW"

            result = FundingOIResult(
                symbol=symbol, funding_rate=round(fr, 6),
                oi_current=round(oi, 2), oi_prev=round(prev_oi, 2),
                oi_change_pct=round(oi_change_pct * 100, 3),
                oi_rising=oi_rising, bias=bias, conviction=conviction,
                long_aligned=long_aligned, short_aligned=short_aligned,
            )
            logger.info(
                "FUNDING/OI | %s | fr=%.4f%% | OI_chg=%.2f%% | bias=%s | conv=%s",
                symbol, fr * 100, oi_change_pct * 100, bias, conviction,
            )
            return result
        except Exception as e:
            logger.warning("FundingOI error %s: %s", symbol, e)
            return None

    async def _get_funding(self, bsym: str) -> float:
        url = f"{BINANCE_FAPI}/fapi/v1/fundingRate"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, params={"symbol": bsym, "limit": 1},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                data = await r.json()
                return float(data[0]["fundingRate"])

    async def _get_oi(self, bsym: str) -> float:
        url = f"{BINANCE_FAPI}/fapi/v1/openInterest"
        async with aiohttp.ClientSession() as s:
            async with s.get(
                url, params={"symbol": bsym},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                data = await r.json()
                return float(data["openInterest"])
