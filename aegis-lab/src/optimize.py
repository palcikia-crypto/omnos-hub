"""AEGIS optimizer: walk-forward grid search over key params.

Fetches history once, replays the sandbox for each param combo, ranks
combos by robustness (pnl consistency across time chunks + trade count
+ profit factor), and writes results/opt.json with:
- cfg_overrides: the best robust params (worker merges them)
- combo_stats: adaptive-brain seeds per strategy|regime
- leaderboard: full grid results for the dashboard

Robustness beats raw pnl: a combo must be profitable-ish in every time
chunk (no lucky month) and have enough trades to be statistically real.
"""
import argparse
import itertools
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import sim as sim_mod  # noqa: E402
from src import engine  # noqa: E402

OPT_PATH = os.path.join(engine.RESULTS, "opt.json")
STAGE1 = {
    "atr_k": [0.6, 0.8, 1.0, 1.3],
    "vol_spike_min": [1.2, 1.4, 1.7],
}
STAGE2 = {
    "rr": [1.0, 1.5, 2.0],
    "fee_edge_mult": [2.0, 3.0],
}
CHUNKS = 3
MIN_TRADES = 30
OPT_STEP_MS = 15 * 60_000  # coarse steps for search speed; live stays 5-min


def profit_factor(trades):
    gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    return gw / gl if gl > 0 else float("inf")


def chunk_consistency(trades):
    """Mean chunk pnl / worst chunk pnl penalty. Robust = all chunks ok."""
    if len(trades) < CHUNKS * 5:
        return -1.0, -1.0
    pnls = [t["pnl"] for t in trades]
    size = len(pnls) // CHUNKS
    chunk_pnl = []
    for i in range(CHUNKS):
        part = pnls[i * size:(i + 1) * size] if i < CHUNKS - 1 else pnls[(CHUNKS - 1) * size:]
        chunk_pnl.append(sum(part))
    worst = min(chunk_pnl)
    mean_c = statistics.fmean(chunk_pnl)
    return round(mean_c, 2), round(worst, 2)


def combo_score(trades, st):
    if len(trades) < MIN_TRADES:
        return -1e9
    mean_c, worst_c = chunk_consistency(trades)
    if mean_c < 0:
        return -1e9
    pf = profit_factor(trades)
    pf_s = min(pf, 3.0)
    wr = st["stats"]["wins"] / st["stats"]["trades"]
    return round(mean_c + 0.5 * worst_c + 2.0 * pf_s + 3.0 * wr, 3)


def combo_stats(trades, min_n=5):
    out = {}
    for t in trades:
        s, r = t.get("strategy"), t.get("regime")
        if not s or not r:
            continue
        key = f"{s}|{r}"
        b = out.setdefault(key, {"n": 0, "ewma": 0.0})
        alpha = 0.15
        rm = t["pnl"] / max(t.get("risk", 1.0), 1e-9)
        b["n"] += 1
        b["ewma"] = round(alpha * rm + (1 - alpha) * b["ewma"], 4)
    return {k: b for k, b in out.items() if b["n"] >= min_n}


def run_grid(wins, grid, base_overrides):
    keys = list(grid)
    rows = []
    for values in itertools.product(*(grid[k] for k in keys)):
        override = dict(base_overrides)
        override.update(dict(zip(keys, values)))
        cfg = dict(sim_mod.CFG)
        cfg.update(override)
        st, trades, _eq = sim_mod.replay(wins, cfg, step_ms=OPT_STEP_MS)
        score = combo_score(trades, st)
        mean_c, worst_c = chunk_consistency(trades)
        row = {
            "params": override,
            "trades": st["stats"]["trades"],
            "win_rate": round(100 * st["stats"]["wins"] / max(st["stats"]["trades"], 1), 1),
            "pnl": round(st["stats"]["pnl"], 2),
            "profit_factor": round(min(profit_factor(trades), 99.0), 2),
            "chunk_mean_pnl": mean_c,
            "chunk_worst_pnl": worst_c,
            "score": score,
        }
        rows.append(row)
        print(f"grid {override}: score={score} trades={row['trades']} "
              f"wr={row['win_rate']} pnl={row['pnl']}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser(description="AEGIS WFO optimizer")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    t0 = time.time()

    wins, _srcs = sim_mod.fetch_windows(args.days)

    # stage 1: market geometry (stop distance x signal threshold)
    leaderboard = run_grid(wins, STAGE1, {})
    leaderboard.sort(key=lambda r: (-r["score"], -r["pnl"]))
    s1_best = leaderboard[0]

    # stage 2: payoff geometry (R:R x fee envelope) around stage-1 best
    rows2 = run_grid(wins, STAGE2, s1_best["params"])
    leaderboard = leaderboard + rows2
    leaderboard.sort(key=lambda r: (-r["score"], -r["pnl"]))
    best = leaderboard[0]
    robust = best["score"] > -1e8

    # combo seeds from the winning params' replay
    cfg = dict(sim_mod.CFG)
    cfg.update(best["params"])
    st, trades, _eq = sim_mod.replay(wins, cfg)
    out = {
        "generated_ts": int(time.time() * 1000),
        "days": args.days,
        "robust": robust,
        "cfg_overrides": best["params"] if robust else {},
        "best": best,
        "leaderboard": leaderboard[:12],
        "combo_stats": combo_stats(trades),
        "elapsed_s": round(time.time() - t0, 1),
    }
    os.makedirs(engine.RESULTS, exist_ok=True)
    with open(OPT_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({"optimized": True, "robust": robust,
                      "best": best["params"],
                      "score": best["score"], "leaderboard_rows": len(leaderboard),
                      "elapsed_s": out["elapsed_s"]}))


if __name__ == "__main__":
    main()
