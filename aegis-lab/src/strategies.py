"""Strategy library: edge candidates per market regime.

Each strategy gets the feature snapshot + config + regime label and
returns "long" | "short" | None. The adaptive brain (engine.adapt)
decides which strategy|regime combos are allowed to trade, so losing
combos disable themselves and winning ones re-enable — the system
adapts to the market in real time.
"""
from src import regime as rg
from src.features import fee_edge_ok as _distance_ok


def baseline(feat, cfg, r):
    """Volume spike + EMA alignment + momentum (original AEGIS rule)."""
    if feat["vol_spike"] < cfg["vol_spike_min"]:
        return None
    if not _distance_ok(cfg, feat):
        return None
    up = feat["ema_fast"] > feat["ema_slow"] and feat["momentum"] > 0
    dn = feat["ema_fast"] < feat["ema_slow"] and feat["momentum"] < 0
    if up:
        return "long"
    if dn:
        return "short"
    return None


def mean_reversion(feat, cfg, r):
    """Fade Bollinger extremes with RSI confirmation — range regime only.

    Research: BB touch + RSI extreme + ADX<25 filter is the canonical
    range edge; in trends the same signal is a continuation trap.
    """
    if r != rg.RANGE:
        return None
    if not _distance_ok(cfg, feat):
        return None
    z = feat.get("bb_z")
    rsi = feat.get("rsi14")
    if z is None or rsi is None:
        return None
    z_thr = cfg.get("bb_z_entry", 2.2)
    if z <= -z_thr and rsi <= cfg.get("rsi_os", 30):
        return "long"
    if z >= z_thr and rsi >= cfg.get("rsi_ob", 70):
        return "short"
    return None


def breakout(feat, cfg, r):
    """Donchian 15m channel breakout with volume confirmation.

    Research: skip breakouts against the higher-TF trend; require
    expansion (vol spike); best in volatile/trend regimes.
    """
    if r not in (rg.VOLATILE, rg.TREND_UP, rg.TREND_DOWN):
        return None
    if not _distance_ok(cfg, feat):
        return None
    hi, lo = feat.get("don_hi"), feat.get("don_lo")
    if hi is None or lo is None:
        return None
    if feat["vol_spike"] < cfg.get("breakout_vol_min", 1.3):
        return None
    c = feat["close"]
    if c > hi and r != rg.TREND_DOWN:
        return "long"
    if c < lo and r != rg.TREND_UP:
        return "short"
    return None


def trend_follow(feat, cfg, r):
    """Momentum continuation in established trends (ADX-confirmed)."""
    if r not in (rg.TREND_UP, rg.TREND_DOWN):
        return None
    if not _distance_ok(cfg, feat):
        return None
    if feat["vol_spike"] < cfg.get("trend_vol_min", 1.1):
        return None
    up = r == rg.TREND_UP and feat["momentum"] > 0
    dn = r == rg.TREND_DOWN and feat["momentum"] < 0
    if up:
        return "long"
    if dn:
        return "short"
    return None


REGISTRY = {
    "baseline": baseline,
    "mean_reversion": mean_reversion,
    "breakout": breakout,
    "trend_follow": trend_follow,
}


def combo_key(strategy, r):
    return f"{strategy}|{r}"


def combo_allowed(adapt, strategy, r, cfg):
    """Adaptive gate: combos trade until clearly bad (deep negative ewma)."""
    if not adapt:
        return True
    b = adapt.get(combo_key(strategy, r))
    if b is None or b.get("n", 0) < cfg.get("adapt_min_sample", 10):
        return True
    return b.get("ewma", 0.0) > cfg.get("adapt_disable_below", -0.35)


def strategy_order(adapt):
    """Best ewma first, unknown combos last (stable)."""
    names = list(REGISTRY)

    def score(n):
        if not adapt:
            return (1, 0.0)
        best = None
        for k, b in adapt.items():
            if k.startswith(n + "|"):
                v = b.get("ewma", 0.0)
                best = v if best is None else max(best, v)
        if best is None:
            return (1, 0.0)
        return (0, -best)

    return sorted(names, key=score)


def decide(feat, cfg, adapt=None):
    """Try strategies in adapt-aware priority order.

    Combos trade unless clearly bad (ewma below adapt_disable_below).
    Blocked combos accumulate a probe counter; every N-th blocked signal
    is traded anyway (rate-limited exploration) so the brain keeps
    sampling and can detect regime changes / recover combos.

    Returns (side, strategy_name, regime) or None.
    """
    if feat is None or feat["atr15"] is None:
        return None
    r = rg.classify(feat, cfg)
    probe_every = cfg.get("adapt_probe_every", 40)
    order = strategy_order(adapt)
    for name in order:
        side = REGISTRY[name](feat, cfg, r)
        if not side:
            continue
        if combo_allowed(adapt, name, r, cfg):
            return side, name, r
        if adapt is not None:
            b = adapt.get(combo_key(name, r))
            if b is not None:
                b["blocked"] = b.get("blocked", 0) + 1
                if b["blocked"] >= probe_every:
                    b["blocked"] = 0
                    return side, name, r
        break  # best-ranked firing strategy probes; don't cascade
    return None
