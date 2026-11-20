"""AEGIS sandbox: replay historical 1m bars through the live engine.

Same engine, same config, same fees/risk/breakers as the paper worker —
but on history, so a multi-day training dataset accumulates in minutes
instead of weeks. Writes to results/sim_* files only; live paper state
is never touched. Trades are prefixed "sim-" and marked source="sim".
"""
import argparse
import bisect
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import market, features, strategies, universe  # noqa: E402
from src import ai_dev  # noqa: E402
import src.engine as engine  # noqa: E402

with open(os.path.join(HERE, "config", "settings.json")) as f:
    CFG = json.load(f)

SIM_STATE_PATH = os.path.join(engine.RESULTS, "sim_state.json")
SIM_TRADES_PATH = os.path.join(engine.RESULTS, "sim_trades.jsonl")
SIM_EQUITY_PATH = os.path.join(engine.RESULTS, "sim_equity.csv")
WARMUP_BARS = 800


def fresh_state():
    return {
        "equity": CFG["start_equity"],
        "day_key": None,
        "day_start_equity": None,
        "consec_losses": 0,
        "paused_until": 0,
        "halted_until": 0,
        "positions": [],
        "adapt": {},
        "stats": {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0},
        "updated": 0,
    }


def save_sim(trades, st, eq_rows):
    os.makedirs(engine.RESULTS, exist_ok=True)
    with open(SIM_TRADES_PATH, "w") as f:
        for t in trades:
            f.write(json.dumps(t) + "\n")
    st["updated"] = int(time.time() * 1000)
    with open(SIM_STATE_PATH, "w") as f:
        json.dump(st, f, indent=1)
    with open(SIM_EQUITY_PATH, "w") as f:
        f.write("\n".join(f"{ts},{eq:.4f}" for ts, eq in eq_rows) + "\n")


class Window:
    """Ascending bars with O(log n) slice-up-to-ts."""

    def __init__(self, bars):
        self.bars = bars
        self.ts = [b["ts"] for b in bars]

    def upto(self, ts_ms):
        i = bisect.bisect_right(self.ts, ts_ms)
        return self.bars[:i]


def active_symbols(cfg):
    """Auto-universe: the system downloads the coins it needs itself."""
    if cfg.get("auto_universe", True):
        return universe.load_universe(n=cfg.get("universe_size", 8))
    return cfg["symbols"]


def fetch_windows(days, symbols=None):
    symbols = symbols or active_symbols(CFG)
    wins = {}
    srcs = {}
    for sym in symbols:
        bars, src = market.fetch_history(sym, days + 1)
        wins[sym] = Window(bars)
        srcs[sym] = src
        print(f"history {sym}: {len(bars)} bars from {src}", flush=True)
        time.sleep(0.3)
    return wins, srcs


def replay(wins, cfg, start_ts=None, end_ts=None, collect_trades=True,
           symbols=None, step_ms=5 * 60_000):
    """Run the engine over history. Returns (state, trades, eq_rows)."""
    symbols = symbols or list(wins)
    if start_ts is None:
        start_ts = min(w.ts[WARMUP_BARS] for w in wins.values())
    if end_ts is None:
        end_ts = max(w.ts[-1] for w in wins.values())

    st = fresh_state()
    trades = []
    eq_rows = []
    last_eq = start_ts

    for now_ms in range(start_ts, end_ts, step_ms):
        windows = {sym: w.upto(now_ms) for sym, w in wins.items()}
        engine.resolve_positions(st, trades, windows, cfg, now_ms)
        engine.roll_day(st, now_ms)
        ok, _reason = engine.breakers_ok(st, cfg, now_ms)
        if ok:
            for sym in symbols:
                if len(st["positions"]) >= cfg["max_concurrent"]:
                    break
                if any(p["symbol"] == sym for p in st["positions"]):
                    continue
                w = windows[sym]
                if len(w) <= WARMUP_BARS or w[-1]["ts"] < now_ms - 5 * 60_000:
                    continue
                feat = features.features(w[-WARMUP_BARS:], cfg)
                decision = strategies.decide(feat, cfg, st.get("adapt"))
                if decision:
                    allow_m, _why = ai_dev.filter_decision(feat, cfg, decision)
                    if allow_m:
                        side, strat, reg = decision
                        engine.open_positions(st, cfg, sym, feat, side, now_ms,
                                              "sim", strategy=strat, regime=reg)
        if now_ms - last_eq >= 30 * 60_000:
            eq_rows.append((now_ms, st["equity"]))
            last_eq = now_ms

    eq_rows.append((end_ts, st["equity"]))
    if collect_trades:
        for t in trades:
            t["id"] = "sim-" + t["id"]
    return st, trades, eq_rows


def run_sim(days, seed_equity=None):
    t0 = time.time()
    symbols = active_symbols(CFG)
    universe.apply_specs(CFG, symbols)
    wins, _srcs = fetch_windows(days, symbols)

    engine.STATE_PATH = SIM_STATE_PATH
    engine.TRADES_PATH = SIM_TRADES_PATH
    engine.EQUITY_PATH = SIM_EQUITY_PATH

    st, trades, eq_rows = replay(wins, CFG)
    if seed_equity is not None:
        st["equity"] = float(seed_equity)
    save_sim(trades, st, eq_rows)

    s = st["stats"]
    wr = (s["wins"] / s["trades"] * 100) if s["trades"] else 0.0
    by_strat = {}
    for t in trades:
        k = t.get("strategy") or "baseline"
        b = by_strat.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] = round(b["pnl"] + t.get("pnl", 0.0), 2)
        if t.get("pnl", 0) > 0:
            b["wins"] += 1
    result = {
        "days": days,
        "trades": s["trades"],
        "wins": s["wins"],
        "losses": s["losses"],
        "win_rate": round(wr, 1),
        "pnl": round(s["pnl"], 2),
        "equity": round(st["equity"], 2),
        "by_strategy": by_strat,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(json.dumps(result), flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description="AEGIS sandbox replay")
    ap.add_argument("--days", type=int, default=30,
                    help="history depth in days (default 30)")
    ap.add_argument("--seed-equity", type=float, default=None,
                    help="starting equity override")
    args = ap.parse_args()
    run_sim(args.days, args.seed_equity)


if __name__ == "__main__":
    main()
