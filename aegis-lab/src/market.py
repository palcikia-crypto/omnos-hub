"""AEGIS-Lab: market data ingestion with source fallback.

Primary: Bybit V5 public REST (api.bytick.com -> api.bybit.com).
Fallback: OKX public candles (same instruments, linear swaps).
No API keys needed for public market data.
"""
import json
import os
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(HERE, "config", "settings.json")) as f:
    CFG = json.load(f)

UA = {"User-Agent": "aegis-lab/1.0 (paper research)"}
TIMEOUT = 15


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def fetch_klines_bybit(symbol, interval="1", limit=200):
    """Returns ascending list of bars: {ts,o,h,l,c,v}. ts in ms."""
    last_err = None
    for host in ("api.bytick.com", "api.bybit.com"):
        url = (f"https://{host}/v5/market/kline?category=linear"
               f"&symbol={symbol}&interval={interval}&limit={limit}")
        try:
            data = _get_json(url)
            lst = data.get("result", {}).get("list") or []
            if not lst:
                last_err = f"{host}: empty list, retCode={data.get('retCode')}"
                continue
            bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
                     "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
                    for b in lst]
            bars.sort(key=lambda x: x["ts"])
            return bars, host
        except Exception as e:  # network/geo/parse
            last_err = f"{host}: {e}"
    raise RuntimeError(f"bybit klines failed for {symbol}: {last_err}")


def fetch_klines_binance(symbol, interval="1m", limit=500):
    """Binance USDT-M futures public klines (no key). Max limit 1500."""
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
           f"&interval={interval}&limit={min(limit, 1500)}")
    rows = _get_json(url)
    if not rows:
        raise RuntimeError(f"binance klines empty for {symbol}")
    bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
            for b in rows]
    bars.sort(key=lambda x: x["ts"])
    return bars, "binance"


def fetch_klines_okx(symbol, limit=200):
    inst = symbol.replace("USDT", "-USDT-SWAP")
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={inst}&bar=1m&limit={min(limit, 300)}")
    data = _get_json(url)
    rows = data.get("data") or []
    if not rows:
        raise RuntimeError(f"okx klines empty for {symbol}: {data}")
    bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
            for b in rows]
    bars.sort(key=lambda x: x["ts"])
    return bars, "okx"


def fetch_klines(symbol, limit=200):
    """Try configured sources in order; return (bars, source_name)."""
    errors = []
    for src in CFG["data_sources"]:
        try:
            if src == "binance":
                return fetch_klines_binance(symbol, limit=limit)
            if src in ("bytick", "bybit"):
                return fetch_klines_bybit(symbol, limit=limit)
            if src == "okx":
                return fetch_klines_okx(symbol, limit=limit)
        except Exception as e:
            errors.append(str(e))
            time.sleep(0.5)
    raise RuntimeError(f"all sources failed for {symbol}: " + " | ".join(errors))


def fetch_klines_binance_range(symbol, days, interval="1m", bars_per_day=1440):
    """Paginated Binance futures history walk backwards via endTime."""
    by_ts = {}
    end_ms = int(time.time() * 1000)
    need = days * bars_per_day
    empty_streak = 0
    while len(by_ts) < need:
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}"
               f"&interval={interval}&limit=1500&endTime={end_ms}")
        rows = _get_json(url)
        if not rows:
            empty_streak += 1
            if empty_streak >= 3:
                break
            time.sleep(1.5)
            continue
        empty_streak = 0
        for b in rows:
            by_ts[int(b[0])] = b
        oldest = min(int(b[0]) for b in rows)
        if oldest >= end_ms:
            break
        end_ms = oldest - 1
        time.sleep(0.2)
    if len(by_ts) < 300:
        raise RuntimeError(f"binance range short for {symbol}: {len(by_ts)}")
    bars = [{"ts": ts, "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
            for ts, b in by_ts.items()]
    bars.sort(key=lambda x: x["ts"])
    return bars, "binance"


def _bars_from_bybit_list(lst):
    bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
            for b in lst]
    bars.sort(key=lambda x: x["ts"])
    return bars


def fetch_klines_bybit_range(symbol, days, interval="1", bars_per_day=1440):
    """Paginated history walk backwards: ~days of 1m bars, ascending."""
    by_ts = {}
    end_ms = int(time.time() * 1000)
    need = days * bars_per_day
    last_err = None
    for host in ("api.bytick.com", "api.bybit.com"):
        by_ts.clear()
        try:
            empty_streak = 0
            while len(by_ts) < need:
                url = (f"https://{host}/v5/market/kline?category=linear"
                       f"&symbol={symbol}&interval={interval}&limit=1000"
                       f"&end={end_ms}")
                lst = _get_json(url).get("result", {}).get("list") or []
                if not lst:
                    empty_streak += 1
                    if empty_streak >= 3:
                        break
                    time.sleep(1.5)
                    continue
                empty_streak = 0
                for b in lst:
                    by_ts[int(b[0])] = b
                oldest = min(int(b[0]) for b in lst)
                if oldest >= end_ms:
                    break
                end_ms = oldest - 1
                time.sleep(0.25)
            if len(by_ts) >= 300:
                return _bars_from_bybit_list(by_ts.values()), host
            last_err = f"{host}: only {len(by_ts)} bars"
        except Exception as e:
            last_err = f"{host}: {e}"
    raise RuntimeError(f"bybit range failed for {symbol}: {last_err}")


def fetch_klines_okx_range(symbol, days, bar="1m", bars_per_day=1440):
    """OKX history-candles pagination (100 bars/page)."""
    inst = symbol.replace("USDT", "-USDT-SWAP")
    by_ts = {}
    after = ""
    need = days * bars_per_day
    while len(by_ts) < need:
        url = (f"https://www.okx.com/api/v5/market/history-candles"
               f"?instId={inst}&bar={bar}&limit=100{after}")
        rows = _get_json(url).get("data") or []
        if not rows:
            break
        for b in rows:
            by_ts[int(b[0])] = b
        oldest = min(int(b[0]) for b in rows)
        after = f"&after={oldest}"
        time.sleep(0.15)
    if len(by_ts) < 300:
        raise RuntimeError(f"okx range short for {symbol}: {len(by_ts)} bars")
    bars = [{"ts": ts, "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])}
            for ts, b in by_ts.items()]
    bars.sort(key=lambda x: x["ts"])
    return bars, "okx"


TF_MAP = {"1m": ("1m", "1", "1m"), "15m": ("15m", "15", "15m"),
          "1h": ("1h", "60", "1H"), "4h": ("4h", "240", "4H")}


def tf_bars_per_day(tf):
    return {"1m": 1440, "15m": 96, "1h": 24, "4h": 6}.get(tf, 1440)


def fetch_history(symbol, days, tf="1m"):
    """Historical range for the sandbox; same source order as live.

    Поддержанные ТФ: 1m/15m/1h/4h — лаборатория качает длинные окна
    на старших ТФ, чтобы проверить устойчивость стратегии за годы.
    """
    iv_binance, iv_bybit, iv_okx = TF_MAP[tf]
    bpd = tf_bars_per_day(tf)
    errors = []
    for src in CFG["data_sources"]:
        try:
            if src == "binance":
                return fetch_klines_binance_range(symbol, days, interval=iv_binance,
                                                  bars_per_day=bpd)
            if src in ("bytick", "bybit"):
                return fetch_klines_bybit_range(symbol, days, interval=iv_bybit,
                                                bars_per_day=bpd)
            if src == "okx":
                return fetch_klines_okx_range(symbol, days, bar=iv_okx,
                                              bars_per_day=bpd)
        except Exception as e:
            errors.append(str(e))
            time.sleep(0.5)
    raise RuntimeError(f"all history sources failed for {symbol}: "
                       + " | ".join(errors))
