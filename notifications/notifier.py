"""
=============================================================================
  DISCORD NOTIFICATION ENGINE — Binance Futures Signal Alerts
=============================================================================
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
import aiohttp
import config

logger = logging.getLogger("discord")


class DiscordNotifier:
    def __init__(self, webhook: str = config.DISCORD_WEBHOOK_URL):
        self.webhook = webhook

    async def send_signal(self, sig, sizing, ks) -> bool:
        if not self.webhook:
            logger.warning("Discord webhook not configured")
            return False

        color  = 0x00FF88 if sig.direction == "LONG" else 0xFF4455
        emoji  = "🟢 LONG" if sig.direction == "LONG" else "🔴 SHORT"
        state  = sig.locker_status if hasattr(sig, "locker_status") else {}

        # ── Funding & OI block ────────────────────────────────────────────
        fr_line = ""
        if sig.funding:
            fr = sig.funding.funding_rate * 100
            fr_line = (
                f"\n> **Funding Rate**: `{fr:+.4f}%` "
                f"({'🟢 Favors LONG' if fr <= 0 else '🔴 Favors SHORT'})"
                f"\n> **Open Interest**: `{sig.funding.oi_change_pct:+.2f}%` change "
                f"({'📈 Rising' if sig.funding.oi_rising else '📉 Flat'})"
            )

        # ── OBI + CVD block ───────────────────────────────────────────────
        obi_line = ""
        if sig.obi:
            obi_line = f"\n> **OBI Ratio**: `{sig.obi.ratio:.2f}:1` | Score: `{sig.obi.imbalance_score:+.3f}`"
        if sig.cvd:
            obi_line += f"\n> **CVD Z-Score**: `{sig.cvd.cvd_zscore:+.2f}σ` {'🔥 SPIKE' if sig.cvd.spike else ''}"

        # ── Sweep block ───────────────────────────────────────────────────
        sweep_line = ""
        if sig.sweep and sig.sweep.is_actionable:
            sweep_line = (
                f"\n> **Liq Sweep**: `{sig.sweep.sweep_tf}` "
                f"`{sig.sweep.sweep_level:.4f}` "
                f"(weight: {sig.sweep.sweep_weight} pts)"
            )

        desc = (
            f"## {sig.label}  ·  {emoji}\n"
            f"**Score**: `{sig.score}/100`\n\n"
            f"```\n"
            f"Entry Zone : {sig.entry_price:.4f}\n"
            f"Stop Loss  : {sig.sl_price:.4f if sig.sl_price else 'N/A'}\n"
            f"TP 1 (1R)  : {sig.tp1:.4f if sig.tp1 else 'N/A'}  ← close 40%\n"
            f"TP 2 (2R)  : {sig.tp2:.4f if sig.tp2 else 'N/A'}  ← close 40%\n"
            f"TP 3 (3.5R): {sig.tp3:.4f if sig.tp3 else 'N/A'}  ← close 20%\n"
            f"R:R Ratio  : 1 : {sig.rr_ratio:.2f}\n"
            f"Leverage   : {sig.leverage}x  (isolated)\n"
            f"Risk       : ${sizing.risk_usdt:.2f}  ({sizing.risk_pct:.2f}%)\n"
            f"Size       : ${sizing.position_usdt:.0f} notional\n"
            f"```"
            f"{sweep_line}{obi_line}{fr_line}"
        )

        breakdown = "\n".join(
            f"• {k.replace('_',' ').title()}: **{v}** pts"
            for k, v in sig.breakdown.items()
        )

        payload = {
            "username":   "⚔️ Predatory Binance Engine",
            "embeds": [{
                "title":       f"📡 {sig.symbol.split(':')[0]}  ·  Futures",
                "description": desc,
                "color":       color,
                "fields": [
                    {
                        "name":   "📊 Score Breakdown",
                        "value":  breakdown or "N/A",
                        "inline": False,
                    },
                    {
                        "name":   "⚠️ Risk Management",
                        "value":  (
                            f"Use **Isolated Margin** · "
                            f"Set SL **immediately** after entry · "
                            f"**Never** move SL against position"
                        ),
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": (
                        f"⚔️ Binance Futures Engine · "
                        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
                        f"Signal {ks.get('today_count',1)}/{config.MAX_SIGNALS_PER_DAY} today"
                    )
                },
            }],
        }

        return await self._post(payload)

    async def send_kill_switch(self, ks_status: dict) -> bool:
        payload = {
            "username": "⚔️ Predatory Binance Engine",
            "embeds": [{
                "title":       "🚨 KILL SWITCH TRIPPED",
                "description": (
                    f"Daily drawdown limit hit.\n"
                    f"**Daily P&L**: ${ks_status.get('daily_pnl', 0):+.2f}\n"
                    f"**All signal generation halted until tomorrow.**"
                ),
                "color": 0xFF0000,
            }],
        }
        return await self._post(payload)

    async def send_text(self, text: str) -> bool:
        return await self._post({
            "content":  text,
            "username": "⚔️ Predatory Binance Engine",
        })

    async def _post(self, payload: dict) -> bool:
        if not self.webhook:
            return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    self.webhook, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status not in (200, 204):
                        body = await r.text()
                        logger.error("Discord %d: %s", r.status, body[:200])
                        return False
                    return True
        except Exception as e:
            logger.error("Discord post failed: %s", e)
            return False
