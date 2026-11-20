"""Лабораторные стратегии второй реальности.

Тот же контракт, что и src/strategies.py: (feat, cfg, regime) -> side|None.
Живут ТОЛЬКО в лаборатории, пока параметр s_<name> == 1 не попадёт в
рекомендацию (промоухн по robust-гейту). Данные для правил — из
features.py: momentum, vol_spike, atr_pct, bb_z, don_hi/don_lo, adx15.
"""
from src import regime as rg
from src.features import fee_edge_ok as _distance_ok


def pump_fade(feat, cfg, r):
    """Pump&dump: аномальный объём + аномальный ход -> входим против движения.

    Pump (дальний выброс вверх) -> short-fade; dump -> long-catch.
    В устойчивых трендах не работает — только VOLATILE/RANGE.
    """
    if cfg.get("s_pump_fade", 0) != 1:
        return None
    if r not in (rg.VOLATILE, rg.RANGE):
        return None
    if not _distance_ok(cfg, feat):
        return None
    spike = feat.get("vol_spike") or 0
    thr = cfg.get("pump_spike_min", 3.2)
    if spike < thr:
        return None
    atr = feat.get("atr15") or 0
    mom = feat.get("momentum") or 0
    if atr <= 0:
        return None
    move = mom / atr  # ход в ATR за последние бары
    if move > cfg.get("pump_move_atr", 2.0):
        return "short"
    if move < -cfg.get("pump_move_atr", 2.0):
        return "long"
    return None


def scalp(feat, cfg, r):
    """Скальпинг: микромоментум по выровненным EMA на спокойном рынке.

    Вход в затяжной ход после отката: RSI нейтральный, без аномального
    объёма, узкий ATR — короткий TTL (ttl из cfg) и строгий Fee-Edge.
    """
    if cfg.get("s_scalp", 0) != 1:
        return None
    if r == rg.VOLATILE:
        return None
    if not _distance_ok(cfg, feat):
        return None
    spike = feat.get("vol_spike") or 99
    if spike > cfg.get("scalp_vol_max", 1.6):
        return None
    rsi = feat.get("rsi14")
    if rsi is None:
        return None
    ef, es = feat["ema_fast"], feat["ema_slow"]
    if ef is None or es is None or es == 0:
        return None
    tilt = (ef - es) / (es or 1.0)
    up = tilt > cfg.get("scalp_tilt_min", 0.0004) and cfg_rsi_band(rsi, lo=45, hi=70)
    dn = -tilt > cfg.get("scalp_tilt_min", 0.0004) and cfg_rsi_band(rsi, lo=30, hi=55)
    if up:
        return "long"
    if dn:
        return "short"
    return None


def cfg_rsi_band(rsi, lo, hi):
    return lo <= rsi <= hi


def squeeze_break(feat, cfg, r):
    """Сжатие Donchian-канала -> прорыв по направлению наклона EMA.

    Классический волатилити-сжатие сетап: чем уже канал относительно
    цены, тем сильнее прорыв. Работает в любом режиме, кроме глубокого
    шума (vol_spike при входе должен быть умеренным).
    """
    if cfg.get("s_squeeze", 0) != 1:
        return None
    if not _distance_ok(cfg, feat):
        return None
    hi, lo = feat.get("don_hi"), feat.get("don_lo")
    close = feat.get("close")
    if hi is None or lo is None or not close:
        return None
    width = (hi - lo) / (close or 1.0)
    if width > cfg.get("squeeze_width_max", 0.004):
        return None
    spike = feat.get("vol_spike") or 0
    if spike < cfg.get("squeeze_vol_min", 1.2):
        return None
    ef, es = feat["ema_fast"], feat["ema_slow"]
    if ef is None or es is None:
        return None
    if close > hi and ef > es:
        return "long"
    if close < lo and ef < es:
        return "short"
    return None


LAB_REGISTRY = {
    "pump_fade": pump_fade,
    "scalp": scalp,
    "squeeze_break": squeeze_break,
}

PARAM_HINTS = {
    "pump_fade": {"pump_spike_min": (2.5, 6.0), "pump_move_atr": (1.2, 4.0)},
    "scalp": {"scalp_vol_max": (1.2, 2.5), "scalp_tilt_min": (0.0001, 0.002)},
    "squeeze_break": {"squeeze_width_max": (0.002, 0.01), "squeeze_vol_min": (1.1, 2.5)},
}


def activate(cfg):
    """Регистрирует лаб-стратегии в боевом реестре на время сессии лабы.

    Стратегии, у которых s_<name> != 1, сразу возвращают None —
    включение packs управляется параметрами варианта (и только ими).
    """
    from src import strategies
    dirty = False
    for name, fn in LAB_REGISTRY.items():
        if name not in strategies.REGISTRY:
            strategies.REGISTRY[name] = fn
            dirty = True
    return dirty
