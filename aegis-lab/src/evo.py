"""AEGIS evolution: mutation + crossover search over strategy params.

Survival is decided by the WFO combo_score (needs profit ACROSS time chunks,
enough trades and a real profit factor) — lucky-window mutants get killed.
Runs inside the daily train job with a hard time budget; the robust winner
goes to results/evo.json and worker merges it below AI overrides.
"""
import argparse
import json
import os
import random
import time

from src import engine
from src import sim as sim_mod
from src import optimize as opt_mod

RESULTS = engine.RESULTS
EVO_PATH = os.path.join(RESULTS, "evo.json")

BOUNDS = {
    "atr_k": (0.4, 1.6, float),
    "rr": (1.0, 2.5, float),
    "vol_spike_min": (1.1, 2.0, float),
    "fee_edge_mult": (2.0, 4.0, float),
    "ttl_min": (180, 600, int),
    "ttl_max": (600, 1800, int),
}
MUT_RATE = 0.4


def _clamp(genome):
    for k, (lo, hi, cast) in BOUNDS.items():
        v = genome.get(k)
        if v is None:
            genome[k] = cast(random.uniform(lo, hi))
        genome[k] = cast(min(max(genome[k], lo), hi))
    if genome["ttl_max"] < genome["ttl_min"] + 120:
        genome["ttl_max"] = min(BOUNDS["ttl_max"][1], genome["ttl_min"] + 120)
    return genome


def _mutate(genome):
    g = dict(genome)
    for _ in range(random.choice((1, 2))):
        k = random.choice(list(BOUNDS))
        lo, hi, cast = BOUNDS[k]
        g[k] = cast(float(g[k]) + random.uniform(-0.15, 0.15) * (hi - lo))
    return _clamp(g)


def _crossover(a, b):
    g = {k: (a[k] if random.random() < 0.5 else b[k]) for k in BOUNDS}
    return _clamp(g)


def _base_genome():
    with open(os.path.join(os.path.dirname(RESULTS), "config", "settings.json")) as f:
        cfg = json.load(f)
    over = {}
    try:
        with open(os.path.join(RESULTS, "opt.json")) as f:
            over = json.load(f).get("cfg_overrides", {})
    except Exception:
        pass
    cfg.update(over)
    g = {}
    for k, (_lo, _hi, cast) in BOUNDS.items():
        v = cfg.get(k)
        g[k] = cast(v) if v is not None else cast(random.uniform(_lo, _hi))
    return _clamp(g)


def _fitness(wins, genome, cur_best):
    with open(os.path.join(os.path.dirname(RESULTS), "config", "settings.json")) as f:
        cfg = json.load(f)
    cfg.update(genome)
    st, trades, _eq = sim_mod.replay(wins, cfg, step_ms=opt_mod.OPT_STEP_MS)
    score = opt_mod.combo_score(trades, st)
    stats = st["stats"]
    return {"params": genome, "score": score,
            "trades": stats.get("trades", 0), "wr": stats.get("wins", 0),
            "pnl": round(st["equity"] - 1000.0, 2)}


def main():
    ap = argparse.ArgumentParser(description="AEGIS evolution search")
    ap.add_argument("--minutes", type=int, default=12)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    deadline = time.time() + args.minutes * 60
    random.seed(int(time.time()))

    wins, _srcs = sim_mod.fetch_windows(args.days)
    base = _base_genome()
    population = [dict(base), _mutate(base), _mutate(base)]
    evals, generations, best = 0, 0, None
    while time.time() < deadline:
        scored = []
        for g in population:
            if time.time() >= deadline:
                break
            try:
                r = _fitness(wins, g, best)
            except Exception as ex:
                r = {"params": g, "score": -1e9, "trades": 0, "wr": 0,
                     "pnl": 0.0, "error": str(ex)[:80]}
            scored.append(r)
            evals += 1
        scored.sort(key=lambda r: (-r["score"], -r["pnl"]))
        best = scored[0]
        generations += 1
        print(f"gen {generations}: best score={best['score']} "
              f"pnl={best['pnl']} trades={best['trades']}", flush=True)
        if time.time() >= deadline:
            break
        elite = [dict(r["params"]) for r in scored[:2]]
        population = elite + [
            _crossover(random.choice(elite), random.choice(elite)) for _ in range(1)]
        population = [
            _mutate(random.choice(elite)) if random.random() < MUT_RATE else g
            for g in population]

    gotscore = best is not None and best["score"] > -1e8
    out = {
        "ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "generations": generations, "evaluations": evals,
        "robust": gotscore, "best": best,
        "window_days": args.days,
    }
    os.makedirs(RESULTS, exist_ok=True)
    with open(EVO_PATH, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"evo: robust={gotscore} best={json.dumps((best or {}).get('params'))}",
          flush=True)


def load_evo_overrides():
    try:
        with open(EVO_PATH) as f:
            data = json.load(f)
        if data.get("robust") and data.get("best", {}).get("params"):
            return data["best"]["params"]
    except Exception:
        pass
    return {}


if __name__ == "__main__":
    main()
