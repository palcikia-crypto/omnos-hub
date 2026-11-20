"""AEGIS PRO auto-universe: the system downloads what it needs itself.

Picks the most liquid USDT-M futures by 24h quote volume from Binance
public API (no key). Excludes leveraged tokens and unstable pairs.
The universe refreshes on a TTL and is persisted to results/universe.json,
so worker / sandbox / optimizer all trade the same auto-selected coins.
"""
import json
import os
import time

from src import market

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIVERSE_PATH = os.path.join(HERE, "results", "universe.json")
EXCLUDE = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
BASE_WARMUP = ("BTCUSDT", "ETHUSDT")  # always included for stability


def _valid_perpetuals():
    """Set of TRADING PERPETUAL symbols from public exchangeInfo."""
    info = market._get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    return {s["symbol"] for s in info.get("symbols", [])
            if s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"}


def fetch_top_symbols(n=8, min_quote_vol=50_000_000):
    """Top-N liquid USDT perps by 24h quote volume."""
    valid = _valid_perpetuals()
    rows = market._get_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
    cands = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT") or sym in EXCLUDE or sym not in valid:
            continue
        try:
            qv = float(r.get("quoteVolume", 0.0))
        except (TypeError, ValueError):
            continue
        if qv >= min_quote_vol:
            cands.append((qv, sym))
    cands.sort(reverse=True)
    syms = [s for _qv, s in cands[:n]]
    for b in BASE_WARMUP:
        if b not in syms:
            syms = [b] + syms[:n - 1]
    return syms


def load_specs(symbols):
    """Auto-download lot step / min qty / min notional per symbol."""
    out = {}
    info = market._get_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
    for s in info.get("symbols", []):
        sym = s.get("symbol")
        if sym not in symbols or s.get("status") != "TRADING":
            continue
        step = min_qty = None
        notional = 5.0
        for flt in s.get("filters", []):
            ft = flt.get("filterType")
            if ft == "LOT_SIZE":
                step = float(flt["stepSize"])
                min_qty = float(flt["minQty"])
            elif ft == "MIN_NOTIONAL":
                try:
                    notional = float(flt.get("notional", 5.0))
                except (TypeError, ValueError):
                    pass
        if step:
            out[sym] = {"step": step, "min_qty": min_qty,
                        "notional": notional}
    return out


def apply_specs(cfg, symbols):
    """Merge downloaded symbol specs into runtime cfg (missing keys only)."""
    try:
        specs = load_specs(symbols)
    except Exception:
        return
    for sym, sp in specs.items():
        cfg.setdefault("qty_step", {}).setdefault(sym, sp["step"])
        cfg.setdefault("min_qty", {}).setdefault(sym, sp["min_qty"])
        cfg.setdefault("min_notional", {}).setdefault(sym, sp["notional"])


def load_universe(max_age_h=12, n=8):
    """Return current symbols; refresh from exchange when stale/missing."""
    if os.path.exists(UNIVERSE_PATH):
        try:
            with open(UNIVERSE_PATH) as f:
                u = json.load(f)
            if time.time() * 1000 - u.get("ts", 0) < max_age_h * 3_600_000:
                return u["symbols"]
        except Exception:
            pass
    try:
        syms = fetch_top_symbols(n=n)
    except Exception:
        syms = list(BASE_WARMUP) + ["SOLUSDT"]
        if os.path.exists(UNIVERSE_PATH):
            try:
                with open(UNIVERSE_PATH) as f:
                    syms = json.load(f)["symbols"]
            except Exception:
                pass
    save_universe(syms)
    return syms


def save_universe(symbols):
    os.makedirs(os.path.dirname(UNIVERSE_PATH), exist_ok=True)
    with open(UNIVERSE_PATH, "w") as f:
        json.dump({"symbols": symbols, "ts": int(time.time() * 1000)}, f,
                  indent=1)
