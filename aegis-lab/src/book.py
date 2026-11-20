"""AEGIS order-book (стакан) checks: real spread + depth imbalance via OKX.

Live microstructure gate: entries with a wide spread get skipped, because
with 1m scalping the round-trip fee + spread can eat the whole edge.
Snapshot is saved to results/book.json for the dashboard.
"""
import json
import os
import time
import urllib.request

from src import engine

RESULTS = engine.RESULTS
BOOK_PATH = os.path.join(RESULTS, "book.json")
UA = {"User-Agent": "aegis-lab/1.0 (book check)"}
TIMEOUT = 10


def _inst(symbol):
    return symbol[:-4] + "-USDT-SWAP" if symbol.endswith("USDT") else symbol


def snapshot(symbol):
    url = (f"https://www.okx.com/api/v5/market/books"
           f"?instId={_inst(symbol)}&sz=5")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode())
    levels = (data.get("data") or [{}])[0]
    asks = [(float(a[0]), float(a[1])) for a in levels.get("asks", [])]
    bids = [(float(b[0]), float(b[1])) for b in levels.get("bids", [])]
    if not asks or not bids:
        raise RuntimeError(f"empty book for {symbol}")
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 1e4
    bid_sz = sum(s for _, s in bids)
    ask_sz = sum(s for _, s in asks)
    imbalance = (bid_sz - ask_sz) / (bid_sz + ask_sz) if (bid_sz + ask_sz) else 0.0
    depth_bid = sum(p * s for p, s in bids)
    depth_ask = sum(p * s for p, s in asks)
    return {
        "symbol": symbol, "bid": bid, "ask": ask,
        "spread_bps": round(spread_bps, 2),
        "imbalance": round(imbalance, 3),
        "depth_bid_usd": round(depth_bid), "depth_ask_usd": round(depth_ask),
        "ts": int(time.time() * 1000),
    }


def gather(symbols):
    out = {}
    for sym in symbols:
        try:
            out[sym] = snapshot(sym)
        except Exception:
            pass
        time.sleep(0.15)
    return out


def spread_gate(cfg, snap):
    return snap["spread_bps"] <= cfg.get("max_spread_bps", 8.0)


def save(book_map):
    os.makedirs(RESULTS, exist_ok=True)
    with open(BOOK_PATH, "w") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                   "books": book_map}, f, ensure_ascii=False, indent=1)
    return BOOK_PATH


def summary(book_map):
    if not book_map:
        return None
    sp = [b["spread_bps"] for b in book_map.values()]
    return {"n": len(sp), "avg_spread_bps": round(sum(sp) / len(sp), 2),
            "max_spread_bps": max(sp)}
