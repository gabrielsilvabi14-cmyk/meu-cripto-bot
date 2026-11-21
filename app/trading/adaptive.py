"""Conservative online learning for PAPER trades; no external ML dependencies.

A rule is activated only after enough closed observations in both score bands,
and only if the higher-score band has better net returns. It never relaxes the
existing >60 threshold or changes hard security filters.
"""
from math import isfinite

MIN_GROUP = 12
MIN_TOTAL = 40
BASE_THRESHOLD = 60
TIGHT_THRESHOLD = 80

def _finite(value):
    try:
        n = float(value)
        return n if isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None

def _stats(rows):
    if not rows:
        return {"count": 0, "wins": 0, "win_rate_pct": None,
                "average_return_pct": None, "net_pnl_usd": 0.0}
    returns = [r["return_pct"] for r in rows]
    return {"count": len(rows), "wins": sum(v > 0 for v in returns),
            "win_rate_pct": round(100 * sum(v > 0 for v in returns) / len(rows), 1),
            "average_return_pct": round(sum(returns) / len(returns), 2),
            "net_pnl_usd": round(sum(r["pnl_usd"] for r in rows), 2)}

def evaluate(events):
    """Closed PAPER_SELL observations, excluding deposits and open marks.

    Multiple partial exits for a token are grouped into one completed trade.
    The entry score is taken from the sell event, not current market data.
    """
    by_token = {}
    for event in events:
        if event.get("action") != "PAPER_SELL":
            continue
        score = _finite(event.get("entry_score"))
        pnl = _finite(event.get("realized_pnl_usd"))
        basis = _finite(event.get("cost_basis_usd"))
        if score is None or pnl is None or basis is None or basis <= 0:
            continue
        token = event.get("token")
        if not token:
            continue
        # Events can include partial exits; aggregate the realized economics.
        key = (token, event.get("timestamp", "")[:10])
        rec = by_token.setdefault(key, {"score": score, "pnl": 0.0, "basis": 0.0})
        rec["pnl"] += pnl
        rec["basis"] += basis
    rows = [{"score": r["score"], "pnl_usd": r["pnl"],
             "return_pct": r["pnl"] / r["basis"] * 100}
            for r in by_token.values() if r["basis"] > 0]
    low = [r for r in rows if BASE_THRESHOLD < r["score"] <= TIGHT_THRESHOLD]
    high = [r for r in rows if r["score"] > TIGHT_THRESHOLD]
    low_stats, high_stats = _stats(low), _stats(high)
    # Avoid reacting to a few random wins or losses. A meaningful difference
    # in both win rate and average net return is required.
    sufficient = len(rows) >= MIN_TOTAL and len(low) >= MIN_GROUP and len(high) >= MIN_GROUP
    higher_better = (sufficient and
        high_stats["average_return_pct"] >= low_stats["average_return_pct"] + 8 and
        high_stats["win_rate_pct"] >= low_stats["win_rate_pct"] + 10 and
        low_stats["average_return_pct"] < 0)
    threshold = TIGHT_THRESHOLD if higher_better else BASE_THRESHOLD
    pattern_stats = analyze_patterns(events)
    return {"pattern_learning": pattern_stats, "mode": "adaptive" if higher_better else "collecting_or_baseline",
            "minimum_score_exclusive": threshold,
            "trades_analyzed": len(rows), "minimum_trades_for_review": MIN_TOTAL,
            "low_score_61_80": low_stats, "high_score_81_100": high_stats,
            "explanation": ("Histórico favorece pontuações >80; limite elevado automaticamente."
                            if higher_better else
                            "Limite original >60 mantido até haver evidência suficiente e consistente."),
            "safety": "Somente PAPER; filtros de segurança existentes preservados."}

def analyze_patterns(events):
    """Descriptive PAPER-only outcomes by observed price/volume patterns.
    Missing candle history is explicitly unclassified; never inferred.
    """
    groups = {}
    for event in events:
        if event.get("action") != "PAPER_SELL":
            continue
        pnl = _finite(event.get("realized_pnl_usd"))
        basis = _finite(event.get("cost_basis_usd"))
        if pnl is None or basis is None or basis <= 0:
            continue
        # Only entry-time candle patterns are usable for learning entry selection:
        # post-entry patterns would leak future information.
        for pattern in event.get("entry_patterns", []) or []:
            if not isinstance(pattern, str):
                continue
            group = groups.setdefault(pattern, [])
            group.append({"return_pct": 100 * pnl / basis, "pnl_usd": pnl})
    return {"status": "observational_not_validated", "source": "completed_1m_ohlcv_entry_patterns",
            "patterns": {k: _stats(v) for k, v in groups.items()},
            "note": "Padrões na entrada, quando OHLCV disponível; resultados descritivos, sem previsão garantida."}
