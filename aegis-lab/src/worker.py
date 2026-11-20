"""AEGIS-Lab worker: fetch -> resolve -> decide (multi-strategy + regime)
-> ML gate -> open -> adapt -> save -> dashboard."""
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import market, features, engine, dashboard  # noqa: E402
from src import strategies, universe  # noqa: E402
from src import ai_advisor  # noqa: E402
from src import ai_dev  # noqa: E402
from src import verify  # noqa: E402
from src import evo  # noqa: E402
from src import book  # noqa: E402
from src import lab as lab_mod  # noqa: E402

OPT_PATH = os.path.join(HERE, "results", "opt.json")
MODEL_PATH = os.path.join(HERE, "results", "model.json")

with open(os.path.join(HERE, "config", "settings.json")) as f:
    CFG = json.load(f)

if os.path.exists(OPT_PATH):
    try:
        with open(OPT_PATH) as f:
            CFG.update(json.load(f).get("cfg_overrides", {}))
    except Exception:
        pass
CFG.update(ai_advisor.load_ai_overrides())
CFG.update(evo.load_evo_overrides())
CFG.update(lab_mod.load_recommend_overrides())


def load_opt():
    if not os.path.exists(OPT_PATH):
        return None
    try:
        with open(OPT_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def load_model():
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        with open(MODEL_PATH) as f:
            return json.load(f)
    except Exception:
        return None


MIN_MODEL_AUC = 0.55


def gate(model, feat, side):
    """ML filter (Этап 3): pass-through unless the model has real edge."""
    if model is None:
        return True, None
    auc = (model.get("wfo") or {}).get("val_auc", 0.0)
    if auc < MIN_MODEL_AUC:
        return True, None
    p = features.ml_prob(model, feat, side)
    return p >= model["threshold"], round(p, 4)


def log_crash(exc):
    os.makedirs(engine.RESULTS, exist_ok=True)
    with open(engine.CRASH_PATH, "a") as f:
        f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} ===\n")
        f.write(traceback.format_exc())


def run_cycle():
    now_ms = int(time.time() * 1000)
    st = engine.load_state(CFG)
    trades = engine.load_trades()

    opt = load_opt()
    if opt and not st.get("adapt"):
        engine.seed_adapt(st, opt.get("combo_stats", {}))

    symbols = universe.load_universe(n=CFG.get("universe_size", 8)) \
        if CFG.get("auto_universe", True) else CFG["symbols"]
    # positions may live on coins that left the universe — resolve them too
    for p in st["positions"]:
        if p["symbol"] not in symbols:
            symbols.append(p["symbol"])
    universe.apply_specs(CFG, symbols)

    symbol_bars = {}
    sources = {}
    for sym in symbols:
        bars, src = market.fetch_klines(sym, limit=800)
        symbol_bars[sym] = bars
        sources[sym] = src
        time.sleep(0.2)

    # 1) resolve open positions against fresh bars (updates adapt brain)
    engine.resolve_positions(st, trades, symbol_bars, CFG, now_ms)

    # 2) day rollover + breakers
    engine.roll_day(st, now_ms)
    ok, reason = engine.breakers_ok(st, CFG, now_ms)

    opened = []
    gated = 0
    book_gated = 0
    book_map = {}
    model = load_model()
    regimes = {}
    if ok:
        for sym in symbols:
            if len(st["positions"]) >= CFG["max_concurrent"]:
                break
            if any(p["symbol"] == sym for p in st["positions"]):
                continue
            bars = symbol_bars[sym]
            if not bars or now_ms - bars[-1]["ts"] > 5 * 60_000:
                continue
            if now_ms - bars[-1]["ts"] < 60_000 and len(bars) > 60:
                bars = bars[:-1]
            if len(bars) < 60:
                continue
            feat = features.features(bars, CFG)
            decision = strategies.decide(feat, CFG, st.get("adapt"))
            if decision:
                allow_m, why = ai_dev.filter_decision(feat, CFG, decision)
                if not allow_m:
                    gated += 1
                    continue
                side, strat, reg = decision
                regimes[sym] = reg
                try:
                    snap = book.snapshot(sym)
                    book_map[sym] = snap
                    if not book.spread_gate(CFG, snap):
                        gated += 1
                        book_gated += 1
                        continue
                except Exception:
                    pass
                allow, p = gate(model, feat, side)
                if not allow:
                    gated += 1
                    continue
                pos = engine.open_positions(st, CFG, sym, feat, side, now_ms,
                                            sources[sym], strategy=strat,
                                            regime=reg)
                if pos:
                    opened.append(f"{pos['id']}:{strat}")

    # 3) persist
    if book_map:
        book.save(book_map)
    engine.save_state(st)
    engine.save_trades(trades)
    engine.append_equity(now_ms, st["equity"])
    try:
        beat = {
            "ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now_ms / 1000)),
            "ts_ms": now_ms,
            "symbols": {s: {"src": sources.get(s), "bars": len(symbol_bars.get(s) or []),
                            "last_ago_m": max(0, int((now_ms - (symbol_bars[s][-1]["ts"] if symbol_bars.get(s) else 0)) / 60000)) if symbol_bars.get(s) else -1}
                        for s in symbols},
            "opened": opened,
            "ml_gated": gated,
            "book_gated": book_gated,
            "breakers": {"ok": bool(ok), "reason": reason},
        }
        with open(os.path.join(engine.RESULTS, "beat.json"), "w") as f:
            json.dump(beat, f)
    except Exception:
        pass
    try:
        lab_cache = os.path.join(engine.RESULTS, "lab", "cache")
        inv = {"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now_ms / 1000)), "files": {}}
        if os.path.isdir(lab_cache):
            for fn in sorted(os.listdir(lab_cache)):
                if not fn.endswith(".json"):
                    continue
                p = os.path.join(lab_cache, fn)
                try:
                    fsz = os.path.getsize(p)
                    with open(p) as f:
                        dj = json.load(f)
                    win = dj.get("windows") or {}
                    nb = sum(len(v) for v in win.values() if isinstance(v, list))
                    inv["files"][fn] = {"bars": nb, "symbols": dj.get("symbols") or [],
                                        "days": dj.get("days"), "mb": round(fsz / 1_000_000, 1),
                                        "dl_ts": dj.get("ts")}
                except Exception:
                    continue
        with open(os.path.join(engine.RESULTS, "lab_data.json"), "w") as f:
            json.dump(inv, f)
    except Exception:
        pass
    dashboard.generate(st, trades, CFG)

    ai_rep = {"ok": False, "error": "not run"}
    ai_dev_rep = {"ok": False, "error": "not run"}
    ml_wfo = (model or {}).get("wfo", {})
    verify_sum = verify.maybe_verify(trades, CFG)
    extra = {
        "sources": sources,
        "ml_gated": gated,
        "book_gated": book_gated,
        "book": book.summary(book_map),
        "ml": {"auc": ml_wfo.get("val_auc"), "accuracy": ml_wfo.get("val_accuracy")} if model else None,
        "sim": st.get("sim_summary"),
        "real_check": verify_sum,
        "bars": symbol_bars,
    }
    try:
        ai_rep = ai_advisor.maybe_run(st, trades, CFG, extra)
    except Exception:
        log_crash(sys.exc_info()[1])
        ai_rep = {"ok": False, "error": "ai crash (см. crash.log)"}
    try:
        ai_dev_rep = ai_dev.maybe_run(st, trades, CFG, extra)
    except Exception:
        log_crash(sys.exc_info()[1])
        ai_dev_rep = {"ok": False, "error": "ai_dev crash (см. crash.log)"}
    try:
        lab_rep = lab_mod.maybe_step(st, trades, CFG, extra)
    except Exception:
        log_crash(sys.exc_info()[1])
        lab_rep = {"ok": False, "error": "lab crash (см. crash.log)"}

    result = {
        "now": now_ms,
        "sources": sources,
        "regimes": regimes,
        "opened": opened,
        "ml_gated": gated,
        "book_gated": book_gated,
        "ml_active": model is not None,
        "adapt_combos": len(st.get("adapt", {})),
        "open_positions": len(st["positions"]),
        "equity": round(st["equity"], 2),
        "breaker": reason,
        "stats": st["stats"],
        "ai": {"ok": ai_rep.get("ok"), "verdict": ai_rep.get("verdict")},
        "ai_dev": {"ok": ai_dev_rep.get("ok"), "modules_active": ai_dev_rep.get("modules_active")},
        "lab": {"ok": lab_rep.get("ok"), "phase": lab_rep.get("phase"),
                "evaluated": lab_rep.get("evaluated"), "planned": lab_rep.get("planned")},
        "verify": verify_sum,
    }
    print(json.dumps(result))
    return result


def main():
    try:
        run_cycle()
    except Exception:
        log_crash(sys.exc_info()[1])
        raise  # never swallow crashes (lesson 2026-11-10..16)


if __name__ == "__main__":
    main()
