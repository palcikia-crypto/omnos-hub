"""Feature engineering on 1m bars (pure python, fast enough for 400 bars).

Multi-timeframe: signals computed on 1m, regime/filter indicators on 15m
resampled bars (research consensus: the edge lives on the higher TF).
"""
import statistics


def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = statistics.fmean(values[:period])
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def sma(values, period):
    if len(values) < period:
        return None
    return statistics.fmean(values[-period:])


def stdev(values, period):
    if len(values) < period:
        return None
    return statistics.pstdev(values[-period:])


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = statistics.fmean(gains[-period:])
    al = statistics.fmean(losses[-period:])
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars, period=14):
    """ATR on the given bars (use 15m-resampled for stability)."""
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.fmean(trs[-period:])


def adx(bars, period=14):
    """Wilder ADX (trend strength) on OHLC bars."""
    if len(bars) < 2 * period + 2:
        return None
    trs, pdms, ndms = [], [], []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        up = h - bars[i - 1]["h"]
        dn = bars[i - 1]["l"] - l
        pdms.append(up if up > dn and up > 0 else 0.0)
        ndms.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    n = period
    tr_s = sum(trs[:n])
    pdm_s = sum(pdms[:n])
    ndm_s = sum(ndms[:n])
    dxs = []
    for i in range(n, len(trs)):
        tr_s = tr_s - tr_s / n + trs[i]
        pdm_s = pdm_s - pdm_s / n + pdms[i]
        ndm_s = ndm_s - ndm_s / n + ndms[i]
        if tr_s <= 0:
            dxs.append(0.0)
            continue
        pdi = 100.0 * pdm_s / tr_s
        ndi = 100.0 * ndm_s / tr_s
        denom = pdi + ndi
        dxs.append(100.0 * abs(pdi - ndi) / denom if denom > 0 else 0.0)
    if len(dxs) < period:
        return None
    a = statistics.fmean(dxs[:period])
    for dx in dxs[period:]:
        a = a + (dx - a) / n
    return a


def resample(bars, minutes=15):
    """Resample ascending 1m bars to N-minute bars."""
    out = []
    bucket_ms = minutes * 60_000
    cur = None
    for b in bars:
        bt = b["ts"] - (b["ts"] % bucket_ms)
        if cur is None or bt != cur["ts"]:
            if cur:
                out.append(cur)
            cur = {"ts": bt, "o": b["o"], "h": b["h"], "l": b["l"],
                   "c": b["c"], "v": b["v"]}
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] += b["v"]
    if cur:
        out.append(cur)
    return out


def atr_percentile(bars15, period=14, lookback=48):
    """Percentile rank (0..1) of current ATR%/price vs recent history."""
    if len(bars15) < period + 2:
        return None
    ratios = []
    for i in range(1, len(bars15)):
        h, l, pc = bars15[i]["h"], bars15[i]["l"], bars15[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        ratios.append(tr / max(bars15[i]["c"], 1e-9))
    hist = ratios[-lookback:-1]
    cur = ratios[-1]
    if not hist:
        return None
    below = sum(1 for r in hist if r <= cur)
    return below / len(hist)


def features(bars, cfg):
    """Compute signal features from ascending 1m bars. Returns dict or None."""
    if len(bars) < 60:
        return None
    closes = [b["c"] for b in bars]
    last = bars[-1]
    f = closes[-cfg["ema_fast"] - 1:]
    s = closes[-cfg["ema_slow"] - 1:]
    ef = ema(f, cfg["ema_fast"])
    es = ema(s, cfg["ema_slow"])
    if ef is None or es is None:
        return None
    mb = cfg["momentum_bars"]
    mom = closes[-1] - closes[-1 - mb]
    vols = [b["v"] for b in bars[-21:-1]]
    vavg = statistics.fmean(vols) if vols else 0.0
    vol_spike = (last["v"] / vavg) if vavg > 0 else 0.0
    bars15 = resample(bars, 15)
    a15 = atr(bars15, cfg["atr_period"])
    r14 = rsi(closes, cfg.get("rsi_period", 14))
    bb_p = cfg.get("bb_period", 20)
    bb_k = cfg.get("bb_k", 2.2)
    m20 = sma(closes, bb_p)
    sd20 = stdev(closes, bb_p)
    bb_z = ((last["c"] - m20) / sd20) if (m20 and sd20) else None
    done = bars15[:-1] if len(bars15) > 1 else bars15
    dp = cfg.get("don_period", 20)
    don_hi = max(b["h"] for b in done[-dp:]) if len(done) >= dp // 2 else None
    don_lo = min(b["l"] for b in done[-dp:]) if len(done) >= dp // 2 else None
    ax = adx(bars15, cfg.get("adx_period", 14))
    apct = atr_percentile(bars15)
    return {
        "close": last["c"],
        "ema_fast": ef,
        "ema_slow": es,
        "momentum": mom,
        "vol_spike": round(vol_spike, 3),
        "atr15": a15,
        "rsi14": round(r14, 2) if r14 is not None else None,
        "bb_z": round(bb_z, 3) if bb_z is not None else None,
        "don_hi": round(don_hi, 6) if don_hi is not None else None,
        "don_lo": round(don_lo, 6) if don_lo is not None else None,
        "adx15": round(ax, 2) if ax is not None else None,
        "atr_pct": round(apct, 3) if apct is not None else None,
        "ts": last["ts"],
    }


def fee_edge_ok(cfg, feat):
    """Fee-Edge Envelope (PRO): target move must dominate round-trip
    fees + spread by a required factor. See engine.fee_edge_ok."""
    dist = cfg["atr_k"] * feat["atr15"]
    close = feat["close"]
    fee_dist = (2 * cfg["fee_rate"] + cfg.get("spread_buffer", 0.0002)) * close
    return dist >= cfg.get("fee_edge_mult", 3.0) * fee_dist


def signal(feat, cfg):
    """Legacy single-rule entry (kept as the baseline strategy)."""
    if feat is None or feat["atr15"] is None:
        return None
    if feat["vol_spike"] < cfg["vol_spike_min"]:
        return None
    d = cfg["atr_k"] * feat["atr15"]
    if d < feat["close"] * cfg["min_atr_frac"]:
        return None
    up = feat["ema_fast"] > feat["ema_slow"] and feat["momentum"] > 0
    dn = feat["ema_fast"] < feat["ema_slow"] and feat["momentum"] < 0
    if up:
        return "long"
    if dn:
        return "short"
    return None


def ml_vector(feat, side):
    """Feature vector for the ML gate (train.py uses the same layout)."""
    c = max(feat["close"], 1e-9)
    rsi = feat.get("rsi14")
    bbz = feat.get("bb_z")
    adx = feat.get("adx15")
    return [
        (feat["ema_fast"] - feat["ema_slow"]) / c,
        feat["momentum"] / c,
        feat["vol_spike"],
        feat["atr15"] / c,
        (rsi / 50.0 - 1.0) if rsi is not None else 0.0,
        bbz if bbz is not None else 0.0,
        (adx / 50.0) if adx is not None else 0.0,
        1.0 if side == "long" else -1.0,
    ]


def ml_prob(model, feat, side):
    """Probability that this signal wins, from a trained model dict."""
    import math
    x = ml_vector(feat, side)
    z = model["bias"]
    for w, v, m, s in zip(model["weights"], x, model["mean"], model["std"]):
        z += w * ((v - m) / s if s > 1e-12 else 0.0)
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
