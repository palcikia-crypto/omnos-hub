"""AEGIS Этап 3: train the ML gate on sandbox + live trade logs.

Pure-python logistic regression (no deps), walk-forward validation and
Monte Carlo on trade PnL. Writes results/model.json; the live worker
uses it to filter low-quality signals. Trains only when enough trades
exist (MIN_TRADES); otherwise keeps the previous model untouched.
"""
import json
import os
import random
import statistics
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import features  # noqa: E402

RESULTS = os.path.join(HERE, "results")
SIM_TRADES_PATH = os.path.join(RESULTS, "sim_trades.jsonl")
LIVE_TRADES_PATH = os.path.join(RESULTS, "trades.jsonl")
MODEL_PATH = os.path.join(RESULTS, "model.json")
MIN_TRADES = 40
WFO_SPLIT = 0.7
MC_RUNS = 1000


def load_trades():
    out = []
    for path in (SIM_TRADES_PATH, LIVE_TRADES_PATH):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    t = json.loads(line)
                    if "pnl" in t:
                        out.append(t)
    seen = set()
    uniq = []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    return uniq


def xy(trades):
    xs, ys = [], []
    for t in trades:
        f = t.get("features")
        if not f or f.get("atr15") in (None, 0):
            continue
        side = t.get("side", "long")
        xs.append(features.ml_vector(f, side))
        ys.append(1.0 if t["pnl"] > 0 else 0.0)
    return xs, ys


def standardize(xs):
    cols = list(zip(*xs))
    mean = [statistics.fmean(c) for c in cols]
    std = [statistics.pstdev(c) for c in cols]
    zs = [[(v - m) / (s if s > 1e-12 else 0.0)
           for v, m, s in zip(row, mean, std)] for row in xs]
    return zs, mean, std


def logistic(xs, ys, epochs=400, lr=0.3, l2=0.01):
    n, d = len(xs), len(xs[0])
    w = [0.0] * d
    b = 0.0
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for row, y in zip(xs, ys):
            z = b + sum(wi * xi for wi, xi in zip(w, row))
            p = 1.0 / (1.0 + pow(2.718281828459045, -max(-30.0, min(30.0, z))))
            g = p - y
            gb += g
            for i in range(d):
                gw[i] += g * row[i]
        for i in range(d):
            w[i] -= lr * (gw[i] / n + l2 * w[i])
        b -= lr * gb / n
    return w, b


def prob(model_w, model_b, row):
    z = model_b + sum(wi * xi for wi, xi in zip(model_w, row))
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + pow(2.718281828459045, -z))


def auc(pairs):
    pos = sorted(p for p, y in pairs if y == 1.0)
    neg = sorted(p for p, y in pairs if y == 0.0)
    if not pos or not neg:
        return None
    import bisect
    wins = sum(bisect.bisect_right(neg, p) for p in pos)
    ties = sum(bisect.bisect_left(neg, p) - bisect.bisect_right(neg, p)
               for p in pos)
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def best_threshold(pairs):
    best_t, best_acc = 0.5, -1.0
    cands = sorted({p for p, _ in pairs} | {0.5})
    for t in cands:
        acc = sum(1.0 for p, y in pairs if (p >= t) == (y == 1.0)) / len(pairs)
        if acc > best_acc:
            best_acc, best_t = acc, t
    return round(best_t, 3), round(best_acc, 3)


def monte_carlo(pnls, runs=MC_RUNS):
    if len(pnls) < 10:
        return None
    rng = random.Random(42)
    finals, maxdds = [], []
    for _ in range(runs):
        seq = pnls[:]
        rng.shuffle(seq)
        eq = 0.0
        peak = 0.0
        dds = 0.0
        for p in seq:
            eq += p
            peak = max(peak, eq)
            dds = max(dds, peak - eq)
        finals.append(eq)
        maxdds.append(dds)
    finals.sort()
    maxdds.sort()
    k = max(0, int(0.05 * len(finals)) - 1)
    return {
        "p5_final_pnl": round(finals[k], 2),
        "p95_max_drawdown": round(maxdds[min(-1, int(0.95 * len(maxdds)))], 2),
        "runs": runs,
    }


def main():
    trades = load_trades()
    xs, ys = xy(trades)
    if len(xs) < MIN_TRADES:
        print(json.dumps({
            "trained": False,
            "reason": f"not enough trades: {len(xs)} < {MIN_TRADES}",
            "trades_available": len(xs),
            "min_required": MIN_TRADES,
        }))
        return

    zs, mean, std = standardize(xs)
    split = int(len(zs) * WFO_SPLIT)
    tr_x, tr_y = zs[:split], ys[:split]
    va_x, va_y = zs[split:], ys[split:]

    w, b = logistic(tr_x, tr_y)
    pairs = [(prob(w, b, row), y) for row, y in zip(va_x, va_y)]
    thr, acc = best_threshold(pairs)
    a = auc(pairs)

    base_wr = sum(va_y) / len(va_y)
    kept = [(p, y) for p, y in pairs if p >= thr]
    kept_wr = (sum(y for _, y in kept) / len(kept)) if kept else None

    model = {
        "weights": [round(v, 6) for v in w],
        "bias": round(b, 6),
        "mean": [round(v, 9) for v in mean],
        "std": [round(v, 9) for v in std],
        "threshold": thr,
        "trained_ts": int(__import__("time").time() * 1000),
        "n_trades": len(xs),
        "wfo": {
            "split": WFO_SPLIT,
            "train_trades": len(tr_x),
            "val_trades": len(va_x),
            "val_accuracy": acc,
            "val_auc": round(a, 3) if a is not None else None,
            "base_win_rate": round(base_wr, 3),
            "kept_win_rate": round(kept_wr, 3) if kept_wr is not None else None,
            "kept_share": round(len(kept) / len(pairs), 3) if pairs else None,
        },
        "monte_carlo": monte_carlo([t["pnl"] for t in trades]),
    }
    with open(MODEL_PATH, "w") as f:
        json.dump(model, f, indent=1)
    print(json.dumps({"trained": True, "model": MODEL_PATH,
                      "n_trades": len(xs), "threshold": thr,
                      "val_accuracy": acc,
                      "val_auc": model["wfo"]["val_auc"],
                      "monte_carlo": model["monte_carlo"]}))


if __name__ == "__main__":
    main()
