"""AEGIS авто-верификатор: каждая закрывшаяся paper-сделка пересчитывается
по реальным 1m-свечам биржи (Binance → Bybit), как будто исполнялась на
реальных деньгах: вход по следующему бару с неблагоприятным проскальзыванием,
полная тейкер-комиссия обе стороны, стоп/таймаут глубже на проскальзывание,
TP без проскальзывания (лимитная заявка). Если TP и SL касаются в одном баре —
консервативно засчитывается убыток.

Результаты: results/real_check.json (по сделкам + сводка), результаты идут
в ИИ-мозги (ai_advisor, ai_dev) для самоулучшения и в панель дашборда.
"""
import json
import os
import time
import urllib.request

from src import engine

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = engine.RESULTS
CHECK_PATH = os.path.join(RESULTS, "real_check.json")

UA = {"User-Agent": "aegis-lab/1.0 (real-verification)"}
TIMEOUT = 15
MAX_KEEP = 300
MAX_PER_CYCLE = 12


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _bars_binance(sym, start_ms, end_ms):
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m"
           f"&startTime={start_ms}&endTime={end_ms}&limit=200")
    rows = _get_json(url)
    return [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])} for b in rows]


def _bars_bybit(sym, start_ms, end_ms):
    url = (f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}"
           f"&interval=1&start={start_ms}&end={end_ms}&limit=200")
    d = _get_json(url)
    lst = (d.get("result") or {}).get("list") or []
    bars = [{"ts": int(b[0]), "o": float(b[1]), "h": float(b[2]),
             "l": float(b[3]), "c": float(b[4]), "v": float(b[5])} for b in lst]
    bars.sort(key=lambda x: x["ts"])
    return bars


def _bars(sym, start_ms, end_ms):
    last_err = None
    for fn in (_bars_binance, _bars_bybit):
        try:
            bars = fn(sym, start_ms, end_ms)
            if bars:
                return bars
            last_err = "empty"
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(f"no real bars for {sym}: {last_err}")


def _real_replay(t, bars, cfg, slip_entry_bps, slip_stop_bps):
    """Реалистичное исполнение по реальным барам. Возвращает (kind, pnl, ambiguous)."""
    side = t["side"]
    direction = 1 if side == "long" else -1
    entry = bars[0]["o"] * (1 + direction * slip_entry_bps / 1e4)
    expiry = t["opened_ts"] + t.get("ttl", 300) * 1000
    ambiguous = False
    exit_price = None
    kind = "timeout"
    for b in bars:
        if side == "long":
            hit_tp = b["h"] >= t["tp"]
            hit_sl = b["l"] <= t["sl"]
        else:
            hit_tp = b["l"] <= t["tp"]
            hit_sl = b["h"] >= t["sl"]
        if hit_tp and hit_sl:
            ambiguous = True
            kind, exit_price = "loss", t["sl"] * (1 - direction * slip_stop_bps / 1e4)
            break
        if hit_tp:
            kind, exit_price = "win", t["tp"]
            break
        if hit_sl:
            kind, exit_price = "loss", t["sl"] * (1 - direction * slip_stop_bps / 1e4)
            break
        if b["ts"] >= expiry:
            kind, exit_price = "timeout", b["c"] * (1 - direction * slip_entry_bps / 1e4)
            break
    if exit_price is None:
        kind, exit_price = "timeout", bars[-1]["c"] * (1 - direction * slip_entry_bps / 1e4)
    fees = (entry + exit_price) * t["qty"] * cfg.get("fee_rate", 0.0006)
    pnl = (exit_price - entry) * direction * t["qty"] - fees
    return kind, round(pnl, 4), ambiguous


def verify_trades(trades, cfg, slip_entry_bps=None, slip_stop_bps=None):
    os.makedirs(RESULTS, exist_ok=True)
    rows = _load_rows()
    checked_ids = {o.get("id") for o in rows}

    slip_entry_bps = cfg.get("slip_entry_bps", 1.0) if slip_entry_bps is None else slip_entry_bps
    slip_stop_bps = cfg.get("slip_stop_bps", 2.0) if slip_stop_bps is None else slip_stop_bps

    real_check = []
    errors = []
    for t in reversed(trades):
        if len(real_check) >= MAX_PER_CYCLE:
            break
        if t.get("id") in checked_ids:
            continue
        if t.get("status") not in ("win", "loss", "timeout"):
            continue
        end = t.get("closed_ts") or t.get("expires_ts")
        if not end:
            continue
        try:
            bars = _bars(t["symbol"], t["opened_ts"], min(end + 60_000, t["opened_ts"] + 3 * 3600_000))
            kind, pnl_real, amb = _real_replay(t, bars, cfg, slip_entry_bps, slip_stop_bps)
        except Exception as e:
            errors.append({"symbol": t.get("symbol"), "error": str(e)[:100]})
            continue
        rows.append({
            "id": t.get("id"), "symbol": t.get("symbol"), "side": t.get("side"),
            "opened": time.strftime("%m-%d %H:%M", time.gmtime(t["opened_ts"] / 1000)),
            "status_recorded": t.get("status"), "kind_real": kind,
            "pnl_recorded": t.get("pnl"), "pnl_real": pnl_real,
            "ambiguous": amb, "match": kind == t.get("status"),
        })
        real_check.append(True)
        checked_ids.add(t.get("id"))
        time.sleep(0.15)

    rows = rows[-MAX_KEEP:]
    summary = _summary(rows, errors)
    out = {"summary": summary, "trades": rows}
    with open(CHECK_PATH, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def _load_rows():
    if not os.path.exists(CHECK_PATH):
        return []
    try:
        with open(CHECK_PATH) as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        return data.get("trades") or []
    return []


def _summary(rows, errors):
    if not rows and not errors:
        return {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "trades": 0}
    tot_rec = sum(o.get("pnl_recorded") or 0 for o in rows)
    tot_real = sum(o.get("pnl_real") or 0 for o in rows)
    amb = sum(1 for o in rows if o.get("ambiguous"))
    upd = [(o.get("pnl_real") or 0) - (o.get("pnl_recorded") or 0) for o in rows]
    return {
        "updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "trades": len(rows),
        "recorded_total": round(tot_rec, 3),
        "real_total": round(tot_real, 3),
        "realism_bias": round(tot_real - tot_rec, 3),
        "avg_bias": round(sum(upd) / len(upd), 3) if upd else 0.0,
        "ambiguous": amb,
        "kind_mismatch": sum(1 for o in rows if not o.get("match")),
        "errors": len(errors),
    }


def maybe_verify(trades, cfg):
    try:
        out = verify_trades(trades, cfg)
        return out.get("summary") or {"trades": 0}
    except Exception:
        return {"trades": 0, "error": "verify failed"}
