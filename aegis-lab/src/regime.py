"""Market regime detection (the edge selector).

Research consensus for 1:1 / short-expiry trading:
- trend regime (ADX >= threshold) -> momentum / breakout strategies
- range regime (ADX low, vol normal) -> mean reversion
- volatile regime (ATR percentile spike) -> breakout only, wider stops
"""
TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
RANGE = "range"
VOLATILE = "volatile"


def classify(feat, cfg):
    """Return regime label for the current feature snapshot."""
    adx = feat.get("adx15")
    apct = feat.get("atr_pct")
    thr = cfg.get("adx_trend", 25.0)
    vol_thr = cfg.get("atr_pct_volatile", 0.85)
    if apct is not None and apct >= vol_thr and (adx is None or adx < thr):
        return VOLATILE
    if adx is not None and adx >= thr:
        if feat["ema_fast"] > feat["ema_slow"]:
            return TREND_UP
        if feat["ema_fast"] < feat["ema_slow"]:
            return TREND_DOWN
    return RANGE
