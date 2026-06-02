"""Sector Strength Engine — Phase 3C.

Funnel position:

    MARKET REGIME          (Phase 3A — market_regime.py)
        ↓
    SECTOR STRENGTH        ← (this module)
        ↓
    TOP STOCKS IN SECTOR   (ranking — partially realised via preference here)
        ↓
    OPTIONS LIQUIDITY      (not yet implemented)
        ↓
    AI SCORING
        ↓
    EXECUTION

After the market regime is known but BEFORE individual stocks are ranked, this
module measures how each sector/theme is performing relative to the SPY/QQQ
blend and ranks every group STRONG / NEUTRAL / WEAK.

It only produces gates and preferences — it never places or hard-blocks a trade
on its own beyond the configured contra action:

  CALL → prefer STRONG sectors; in WEAK sectors reduce size or reject
  PUT  → prefer WEAK sectors;   in STRONG sectors reduce size or reject

Market regime, PUT support, volume/risk gates, DRY_RUN/EXECUTE, paper trading,
the scheduler and Discord alerts are all untouched by this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from config import SECTOR_GROUPS, SECTOR_STRENGTH

# ─────────────────────────────────────────────────────────────
# RANK LABELS
# ─────────────────────────────────────────────────────────────
STRONG = "STRONG"
NEUTRAL = "NEUTRAL"
WEAK = "WEAK"

UNCLASSIFIED = "UNCLASSIFIED"

# Benchmark ETFs the sector returns are measured against.
_BENCHMARKS = ("SPY", "QQQ")


# Symbol → group lookup, built once from config.
def _build_symbol_map() -> dict:
    mapping = {}
    for group, symbols in SECTOR_GROUPS.items():
        for sym in symbols:
            mapping[sym.upper()] = group
    return mapping


_SYMBOL_GROUP = _build_symbol_map()


def classify_symbol(symbol: str) -> str:
    """Return the sector/theme group for a symbol, or UNCLASSIFIED."""
    return _SYMBOL_GROUP.get((symbol or "").upper(), UNCLASSIFIED)


@dataclass
class SectorStrength:
    """Ranked sector strengths plus the gates downstream code applies."""

    groups: dict                         # group -> {return, rel, rank, members}
    benchmark: float | None              # SPY/QQQ blended trailing return (%)
    reasons: list[str] = field(default_factory=list)

    # ── Lookups ──────────────────────────────────────────────
    def group_for(self, symbol: str) -> str:
        return classify_symbol(symbol)

    def rank_for(self, symbol: str) -> str:
        group = self.group_for(symbol)
        info = self.groups.get(group)
        return info["rank"] if info else NEUTRAL

    @property
    def strongest(self) -> list[tuple[str, dict]]:
        ranked = [(g, d) for g, d in self.groups.items() if d.get("rel") is not None]
        return sorted(ranked, key=lambda kv: kv[1]["rel"], reverse=True)

    @property
    def weakest(self) -> list[tuple[str, dict]]:
        return list(reversed(self.strongest))

    # ── Gate ─────────────────────────────────────────────────
    def gate(self, option_type: str, symbol: str) -> dict:
        """Decide how sector strength should treat this directional setup.

        Returns a dict:
          action          — "PREFER" | "ALLOW" | "REDUCE" | "REJECT"
          size_multiplier — sizing factor to apply (1.0 unless reduced/rejected)
          rank            — sector rank for the symbol
          group           — the symbol's sector/theme group
          note            — human-readable explanation
        """
        is_put = str(option_type or "CALL").upper() == "PUT"
        group = self.group_for(symbol)
        rank = self.rank_for(symbol)

        aligned = (rank == STRONG and not is_put) or (rank == WEAK and is_put)
        contra = (rank == WEAK and not is_put) or (rank == STRONG and is_put)

        if aligned:
            return {
                "action": "PREFER", "size_multiplier": 1.0, "rank": rank,
                "group": group,
                "note": f"{option_type} aligned with {rank} sector {group}",
            }
        if contra:
            action = str(SECTOR_STRENGTH.get("contra_action", "REDUCE")).upper()
            if action == "REJECT":
                return {
                    "action": "REJECT", "size_multiplier": 0.0, "rank": rank,
                    "group": group,
                    "note": f"{option_type} rejected against {rank} sector {group}",
                }
            mult = float(SECTOR_STRENGTH.get("contra_size_multiplier", 0.5))
            return {
                "action": "REDUCE", "size_multiplier": mult, "rank": rank,
                "group": group,
                "note": f"{option_type} reduced to {mult:.0%} against {rank} sector {group}",
            }
        # NEUTRAL sector or UNCLASSIFIED → neither helped nor penalised.
        return {
            "action": "ALLOW", "size_multiplier": 1.0, "rank": rank,
            "group": group,
            "note": f"{option_type} in {rank} sector {group}",
        }


def _trailing_returns(symbols: list[str], lookback_days: int) -> dict:
    """Batch-fetch trailing % returns for many symbols in a single download.

    Returns {symbol: pct_return} for every symbol with enough data; missing or
    failed symbols are simply absent from the result.
    """
    returns: dict[str, float] = {}
    if not symbols:
        return returns
    try:
        import yfinance as yf

        data = yf.download(
            tickers=" ".join(symbols),
            period="3mo",
            progress=False,
            group_by="column",
            threads=True,
        )
        if data is None or data.empty:
            return returns

        close = data["Close"] if "Close" in data else data

        # Single-symbol downloads come back as a Series, not a DataFrame.
        if hasattr(close, "columns"):
            columns = list(close.columns)
        else:
            columns = symbols[:1]
            close = close.to_frame(name=columns[0])

        for sym in symbols:
            if sym not in columns:
                continue
            series = close[sym].dropna()
            if len(series) <= lookback_days:
                continue
            past = float(series.iloc[-(lookback_days + 1)])
            last = float(series.iloc[-1])
            if past:
                returns[sym] = round((last - past) / past * 100, 2)
    except Exception as exc:
        logger.warning(f"Sector strength: return fetch failed: {exc}")
    return returns


def get_sector_strength(
    spy_return: float | None = None,
    qqq_return: float | None = None,
) -> SectorStrength:
    """Rank every sector/theme group STRONG / NEUTRAL / WEAK vs the SPY/QQQ blend.

    `spy_return` / `qqq_return` (trailing ~21-day %) may be passed in from the
    market-regime layer to avoid re-fetching the benchmarks; any that are None
    are fetched here. Each group's equal-weighted member return is compared to
    the benchmark blend, and the relative spread is bucketed by the configured
    STRONG/WEAK thresholds.
    """
    lookback = int(SECTOR_STRENGTH.get("lookback_days", 21))

    # Decide which symbols still need fetching (benchmarks + all members).
    member_symbols = sorted({s.upper() for syms in SECTOR_GROUPS.values() for s in syms})
    need = list(member_symbols)
    if spy_return is None:
        need.append("SPY")
    if qqq_return is None:
        need.append("QQQ")

    fetched = _trailing_returns(need, lookback)

    if spy_return is None:
        spy_return = fetched.get("SPY")
    if qqq_return is None:
        qqq_return = fetched.get("QQQ")

    bench_vals = [v for v in (spy_return, qqq_return) if v is not None]
    benchmark = round(sum(bench_vals) / len(bench_vals), 2) if bench_vals else None

    strong_th = float(SECTOR_STRENGTH.get("strong_rel_threshold", 2.0))
    weak_th = float(SECTOR_STRENGTH.get("weak_rel_threshold", 2.0))

    groups: dict[str, dict] = {}
    reasons: list[str] = []

    enabled = SECTOR_STRENGTH.get("enabled", True)

    for group, symbols in SECTOR_GROUPS.items():
        member_returns = {
            s.upper(): fetched[s.upper()]
            for s in symbols
            if s.upper() in fetched
        }
        if not member_returns or benchmark is None or not enabled:
            groups[group] = {
                "return": None, "rel": None, "rank": NEUTRAL,
                "members": list(member_returns.keys()),
            }
            continue

        avg_return = round(sum(member_returns.values()) / len(member_returns), 2)
        rel = round(avg_return - benchmark, 2)
        if rel >= strong_th:
            rank = STRONG
        elif rel <= -weak_th:
            rank = WEAK
        else:
            rank = NEUTRAL

        groups[group] = {
            "return": avg_return,
            "rel": rel,
            "rank": rank,
            "members": list(member_returns.keys()),
        }

    if benchmark is None or not enabled:
        reasons.append("Sector data unavailable or disabled — all groups NEUTRAL")
    else:
        reasons.append(f"Benchmark (SPY/QQQ blend) trailing {lookback}d = {benchmark}%")

    return SectorStrength(groups=groups, benchmark=benchmark, reasons=reasons)


def log_sector_strength(sectors: SectorStrength) -> None:
    """Log the strongest and weakest sectors."""
    ranked = sectors.strongest
    if not ranked:
        logger.info("SECTOR STRENGTH: unavailable — treating all sectors as NEUTRAL")
        return

    strongest = [f"{g} ({d['rank']}, {d['rel']:+.1f}%)" for g, d in ranked[:3]]
    weakest = [f"{g} ({d['rank']}, {d['rel']:+.1f}%)" for g, d in sectors.weakest[:3]]
    logger.info(f"SECTOR STRENGTH | strongest: {', '.join(strongest)}")
    logger.info(f"SECTOR STRENGTH | weakest:   {', '.join(weakest)}")
