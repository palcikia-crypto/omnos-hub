"""AEGIS Лаборатория ("вторая реальность"): автономный ИИ-конструктор.

Полная копия системы живёт в results/lab/: у неё свои параметры, свои
модули-фильтры, свои бэктесты на реальной истории и свой журнал.
Цель — построить вариант системы, который стабильно ОБГОНЯЕТ оригинал
по WFO-скору на paper-торговле. Что сработало — портится в оригинал
(results/lab/recommend.json + запись модуля в главный реестр),
что не сработало — остаётся в журнале как опыт (авто-откат к лучшему).

Бюджетная дисциплина: один шаг за вызов, LLM-запросы ограничены в день,
между планами пауза; очередь экспериментов выполняется без LLM.
Ограничения: код модулей проходит ту же песочницу, что и
AI-архитектор (ai_dev.validate_module); данные скачиваются только с
биржевых API (market.py); портация только при устойчивом превосходстве.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from src import engine
from src import market
from src import sim as sim_mod
from src import optimize as opt_mod
from src import ai_advisor
from src import ai_dev
from src import lab_strats
from src import features
from src import strategies
from src import verify

RESULTS = engine.RESULTS
LAB_DIR = os.path.join(RESULTS, "lab")
STATE_PATH = os.path.join(LAB_DIR, "state.json")
JOURNAL_PATH = os.path.join(LAB_DIR, "journal.jsonl")
REPORT_PATH = os.path.join(LAB_DIR, "report.json")
RECOMMEND_PATH = os.path.join(LAB_DIR, "recommend.json")
CKPT_DIR = os.path.join(LAB_DIR, "checkpoints")
MODULES_REGISTRY = os.path.join(LAB_DIR, "modules.json")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEV_DIR = os.path.join(HERE, "ai_modules")

LLM_DAY_LIMIT = int(os.environ.get("AEGIS_LAB_LLM_DAY", "6"))
PLAN_INTERVAL_S = int(os.environ.get("AEGIS_LAB_PLAN_INTERVAL_S", "2700"))
STEP_TIME_S = int(os.environ.get("AEGIS_LAB_STEP_TIME_S", "150"))
EVAL_DAYS = int(os.environ.get("AEGIS_LAB_DAYS", "5"))
MAX_SYMBOLS = 2
CACHE_DIR = os.path.join(LAB_DIR, "cache")
CACHE_TTL_S = 12 * 3600
PAPER_STATE_PATH = os.path.join(LAB_DIR, "paper_state.json")
PAPER_TRADES_PATH = os.path.join(LAB_DIR, "paper_trades.jsonl")
PAPER_EQ_PATH = os.path.join(LAB_DIR, "paper_equity.csv")
PAPER_CHECK_PATH = os.path.join(LAB_DIR, "paper_check.json")
DEEP_SCAN_INTERVAL_S = 6 * 3600
DEEP_SCAN_TF = "15m"
DEEP_SCAN_DAYS = int(os.environ.get("AEGIS_LAB_DEEP_DAYS", "90"))
PAPER_CHECK_INTERVAL_S = 3600
PATTERNS_PATH = os.path.join(LAB_DIR, "patterns.json")
HUNT_INTERVAL_S = int(os.environ.get("AEGIS_LAB_HUNT_INTERVAL_S", str(3 * 3600)))
HUNT_FWD_BARS = 15
HUNT_MIN_BARS = 150
HUNT_MIN_EDGE = float(os.environ.get("AEGIS_LAB_MIN_EDGE", "0.0008"))
MAX_VARIANTS_PER_STEP = 1
MAX_CHECKPOINTS = 12
MIN_PROMOTE_TRADES = 25
BASELINE_REFRESH_S = 4 * 3600

def merged_bounds():
    from src import evo as evo_mod, lab_strats
    b = dict(ai_dev.DEV_BOUNDS)
    for k, v in evo_mod.BOUNDS.items():
        b[k] = v
    for name in lab_strats.LAB_REGISTRY:
        b[f"s_{name}"] = (0, 1, int)
    for name, ps in lab_strats.PARAM_HINTS.items():
        for k, (lo, hi) in ps.items():
            b[k] = (lo, hi, float)
    b["pump_spike_min"] = (2.5, 6.0, float)
    b["pump_move_atr"] = (1.2, 4.0, float)
    b["scalp_vol_max"] = (1.2, 2.5, float)
    b["scalp_tilt_min"] = (0.0001, 0.002, float)
    b["squeeze_width_max"] = (0.002, 0.01, float)
    b["squeeze_vol_min"] = (1.1, 2.5, float)
    return b


SYSTEM = (
    "Ты — ЛАБОРАТОРИЯ 'Вторая реальность' торговой системы AEGIS (paper-only, "
    "реальные деньги заблокированы). У тебя есть точная копия системы: свои "
    "параметры, свои модули-фильтры, бэктесты на реальной истории бирж. "
    "Твоя единственная цель — построить вариант, который СТАБИЛЬНО обгоняет "
    "оригинал по WFO-скору (прибыль во всех временных чанках, PF>1, depth "
    "трейдов) на paper-торговле. Ты разработчик: можешь предлагать любые "
    "параметры из списка (границы дам), писать свои модули-фильтры сигналов "
    "(python: import math/statistics, экспорт NAME + "
    "filter_signal(feat, cfg) -> (allow: bool, reason); ВЕТО-философия: "
    "блокируй только явно рискованные входы, не глуши всё подряд), "
    "запрашивать данные (символ/дни) — система скачает их сама с бирж. "
    "ЭСКАЛАЦИЯ: тебя вызывают ТОЛЬКО по конкретному триггеру (см. поле эскалация "
    "в досье: находка ML, золотые паттерны охоты, расхождение реальной сверки, "
    "просадка lab-paper, провал deep-scan, серия неудач). Пришли, оцени весь "
    "артефакты досье (паттерны, ML WFO, deep_scan, evo, lab-paper, сверка), "
    "сделай 1-3 конкретных действия и ВЫЙДИ — лимиты дорогие, не повторяй то, "
    "что уже внедрено. Дополнительно можешь заказывать действия списком "
    "\u00abrun\u00bb: [\"retrain_ml\"] \u2014 система сама переобучит ML-гейт на накопленных "
    "сделках и вернёт тебе свежий WFO в следующим цикле."
    "Отвечай СТРОГО валидным JSON без markdown:\n"
    '{"thoughts": "что понял из досье, где искать улучшение (по-русски, до 150 слов)", '
    '"queue": [{"name": "короткое_имя", "params": {"param": число}, '
    '"module": null | {"name": "snake_case", "code": "полный код"}, '
    '"note": "гипотеза"}], "data": [{"symbol": "BTCUSDT", "days": 10}]}\n'
    "Правила: queue длиной 1-3 варианта; каждый вариант меняет 1-4 параметра "
    "и/или добавляет один модуль; имена параметров только из списка границ; "
    "избегай повторов того, что уже провалилось (см. журнал).")


def _now_ms():
    return int(time.time() * 1000)


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def journal(event, **kw):
    row = {"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
           "event": event}
    row.update(kw)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _day_key():
    return time.strftime("%Y%m%d", time.gmtime())


def fresh_state():
    return {"phase": "boot", "steps": 0, "llm_used": 0, "llm_day": _day_key(),
            "last_plan_ts": 0, "baseline_ts": 0, "baseline": None,
            "queue": [], "q_idx": 0, "best": None, "promoted": [],
            "ckpts": 0, "triggers": {}, "ml_seen_ts": 0, "ml_last_auc": None,
            "patterns_seen_ts": "", "hunt_clean": 0, "last_escalation": None}


def load_state():
    st = _load_json(STATE_PATH, None) or fresh_state()
    for k, v in fresh_state().items():
        st.setdefault(k, v)
    return st


def save_state(st):
    _save_json(STATE_PATH, st)


def _clamp_params(params):
    bounds = merged_bounds()
    out, rejected = {}, []
    for k, v in (params or {}).items():
        if k not in bounds:
            rejected.append(f"{k} (не в списке)")
            continue
        lo, hi, cast = bounds[k]
        try:
            out[k] = max(lo, min(hi, cast(float(v))))
        except Exception:
            rejected.append(f"{k} (нечисл.")
    return out, rejected


# ------------------------------------------------------------ baseline cache
def _cached_windows(symbols, days, tf="1m"):
    """История с бирж кэшируется на диск (вне git) — бэктесты в рамках
    сессии не бьют повторно по API. ТФ в ключе кэша: длинные окна
    устойчивости качаются на 15m/1h до 2 лет."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"hist_{days}d_{tf}.json")
    cache = _load_json(path, {})
    if (cache.get("symbols") == symbols and cache.get("days") == days
            and cache.get("tf") == tf
            and _now_ms() - cache.get("ts", 0) < CACHE_TTL_S * 1000
            and cache.get("windows")):
        return {s: sim_mod.Window([
                    dict(zip(("ts", "o", "h", "l", "c", "v"), row))
                    for row in rows])
                for s, rows in cache["windows"].items()}, cache.get("srcs", {})
    wins, srcs = {}, {}
    for sym in symbols:
        bars, src = market.fetch_history(sym, days + 1, tf=tf)
        wins[sym] = sim_mod.Window(bars)
        srcs[sym] = src
        time.sleep(0.3)
    _save_json(path, {
        "ts": _now_ms(), "symbols": symbols, "days": days, "tf": tf,
        "srcs": srcs,
        "windows": {s: [[b["ts"], b["o"], b["h"], b["l"], b["c"], b["v"]]
                        for b in w.bars] for s, w in wins.items()},
    })
    return wins, srcs


def ensure_baseline(deadline=None):
    st_lab = load_state()
    fresh = (_now_ms() - st_lab.get("baseline_ts", 0)) < BASELINE_REFRESH_S * 1000
    if fresh and st_lab.get("baseline"):
        return st_lab["baseline"]
    symbols = _eval_symbols()
    cfg = _main_cfg({})
    wins, _srcs = _cached_windows(symbols, EVAL_DAYS)
    st_sim, trades, _eq = sim_mod.replay(
        wins, cfg, step_ms=opt_mod.OPT_STEP_MS, symbols=symbols)
    base = _pack(st_sim, trades, cfg, "baseline-оригинал")
    st_lab["baseline"] = base
    st_lab["baseline_ts"] = _now_ms()
    save_state(st_lab)
    return base


def _eval_symbols():
    try:
        syms = json.load(open(os.path.join(RESULTS, "universe.json")))
        if isinstance(syms, dict):
            syms = syms.get("symbols") or []
        if isinstance(syms, list) and syms and isinstance(syms[0], str):
            return syms[:MAX_SYMBOLS]
    except Exception:
        pass
    return ["BTCUSDT", "ETHUSDT", "SOLUSDT"][:MAX_SYMBOLS]


def _main_cfg(override):
    with open(os.path.join(HERE, "config", "settings.json")) as f:
        cfg = json.load(f)
    for name, key in (("opt.json", "cfg_overrides"),
                      ("lab/recommend.json", "params")):
        p = os.path.join(RESULTS, name)
        if os.path.exists(p):
            try:
                data = json.load(open(p))
                cfg.update(data.get(key) or {})
            except Exception:
                pass
    cfg.update(ai_advisor.load_ai_overrides())
    cfg.update(override)
    return cfg


def _pack(st_sim, trades, cfg, name):
    s = st_sim["stats"]
    return {
        "name": name,
        "score": opt_mod.combo_score(trades, st_sim),
        "trades": s.get("trades", 0),
        "wins": s.get("wins", 0),
        "losses": s.get("losses", 0),
        "pnl": round(s.get("pnl", 0.0), 2),
        "params": {k: cfg.get(k) for k in merged_bounds()},
    }


# ------------------------------------------- эскалация ИИ (триггеры, без рутин)
TRIG_COOLDOWN = {"critical": 30 * 60, "high": 2 * 3600,
                 "medium": 6 * 3600, "low": 12 * 3600}
TRIG_PRIO = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _ml_snapshot():
    m = _load_json(os.path.join(RESULTS, "model.json"), {})
    if not m:
        return None
    wfo = m.get("wfo") or {}
    return {"ts": m.get("trained_ts", 0), "auc": wfo.get("val_auc"),
            "acc": wfo.get("val_accuracy"), "n": m.get("n_trades"),
            "threshold": m.get("threshold"),
            "kept_wr": wfo.get("kept_win_rate")}


def _fail_streak():
    if not os.path.exists(JOURNAL_PATH):
        return 0
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            rows = f.readlines()[-8:]
    except Exception:
        return 0
    streak = 0
    for r in reversed(rows):
        try:
            ev = json.loads(r).get("event")
        except Exception:
            break
        if ev == "failed":
            streak += 1
        elif ev in ("promoted", "checkpoint"):
            break
    return streak


def _check_triggers(st_lab, extra):
    """Детерминированный слой решает, когда звать ИИ. Каждая находка/аномалия
    — повод для полной аналитики; рутина без триггеров ИИ не тратит."""
    now = _now_ms()
    seen = st_lab.setdefault("triggers", {})
    out = []

    def add(kind, prio, detail):
        if now - int(seen.get(kind, 0)) < TRIG_COOLDOWN[prio] * 1000:
            return
        out.append({"kind": kind, "prio": prio, "detail": detail})
        seen[kind] = now

    ml = _ml_snapshot()
    if ml and ml.get("ts") and ml["ts"] > int(st_lab.get("ml_seen_ts", 0)):
        st_lab["ml_seen_ts"] = ml["ts"]
        prev = st_lab.get("ml_last_auc")
        auc = ml.get("auc") or 0
        if prev is not None and abs(auc - prev) >= 0.03:
            add("ml_shift", "high",
                f"ML-гейт найдил смещение AUC {prev} -> {auc}, n={ml.get('n')}")
        elif auc < 0.5:
            add("ml_weak", "medium", f"ML AUC {auc} ниже случайного — гейт шумит")
        st_lab["ml_last_auc"] = auc

    pats = _load_json(PATTERNS_PATH, {})
    pts = pats.get("ts") or ""
    if pts and pts != st_lab.get("patterns_seen_ts"):
        st_lab["patterns_seen_ts"] = pts
        n = len(pats.get("patterns") or [])
        if n:
            add("gold_found", "high",
                f"охота нашла {n} закономерностей (см. паттерны в досье)")
        else:
            st_lab["hunt_clean"] = int(st_lab.get("hunt_clean", 0)) + 1
            if st_lab["hunt_clean"] >= 3:
                st_lab["hunt_clean"] = 0
                add("hunt_sterile", "medium",
                    "три охоты подряд без находок — пересмотреть пороги/окна")

    rc = (extra.get("real_check") or {}) if isinstance(extra, dict) else {}
    rsum = rc.get("summary") or (rc if rc.get("updated") else None)
    if rsum and int(rsum.get("trades") or 0) >= 5:
        bias = float(rsum.get("realism_bias") or 0)
        mism = int(rsum.get("kind_mismatch") or 0)
        if bias <= -1.0:
            add("realism_drift", "critical",
                f"комиссии/проскальзывание съедают {bias} — овервью стратегий")
        if mism >= 3:
            add("realism_mismatch", "high",
                f"{mism} сделок не сходятся с реальными свечами")

    pchk = _load_json(PAPER_CHECK_PATH, {}).get("summary")
    if pchk and int(pchk.get("trades") or 0) >= 3:
        lbias = float(pchk.get("realism_bias") or 0)
        if lbias <= -0.8:
            add("lab_paper_drift", "critical",
                f"lab-paper: реальность хуже записи на {lbias}")

    ps = _load_json(PAPER_STATE_PATH, {})
    if ps:
        eq = float(ps.get("equity") or 1000.0)
        if eq < 950.0 and eq < float(ps.get("day_start_equity") or 1000.0) * 0.97:
            add("lab_paper_drawdown", "critical",
                f"lab-paper equity {eq:.2f} — конфиг лабы теряет")

    ds = st_lab.get("deep_scan") or {}
    if ds.get("verdict") == "fail" and not seen.get("deep_scan_fail"):
        add("deep_scan_fail", "medium",
            f"прогон устойчивости провален ({ds.get('days')}д) — пересобрать")

    if _fail_streak() >= 4:
        add("exp_failures", "medium", "4+ эксперимента подряд провалены")

    out.sort(key=lambda t: TRIG_PRIO[t["prio"]])
    return out


def _run_ai_action(action, st_lab, deadline):
    """ИИ заказал детерминированное действие — система исполняет сама."""
    if action == "retrain_ml" and time.time() < deadline - 30:
        try:
            r = subprocess.run([sys.executable, "-m", "src.train"],
                               cwd=HERE, capture_output=True, text=True,
                               timeout=180)
            data = ai_advisor._parse_json(r.stdout or "") or {}
            ml = _ml_snapshot()
            journal("ml_retrained", trained=bool(data.get("trained")),
                    auc=ml and ml.get("auc"), acc=ml and ml.get("acc"),
                    detail=("ML-гейт переобучен по заказу ИИ, "
                            f"n={ml and ml.get('n')}"))
            st_lab["ml_seen_ts"] = (ml or {}).get("ts", 0)
        except Exception as e:
            journal("ml_retrain_error", detail=f"{type(e).__name__}: {str(e)[:140]}")


# ------------------------------------------------------------ LLM план
def _llm_call(prompt):
    tok = os.environ.get("WAI_TOKEN")
    acc = os.environ.get("WAI_ACCOUNT")
    if not tok or not acc:
        return None, "нет WAI_TOKEN/WAI_ACCOUNT"
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
            "max_tokens": 2400, "temperature": 0.5}
    errs = []
    for m in ai_advisor.MODELS:
        try:
            r = ai_advisor._ai_run(acc, tok, m, body)
            if not r.get("success"):
                errs.append(str(r.get("errors"))[:80])
                continue
            res = r.get("result") or {}
            text = res.get("response") if isinstance(res.get("response"), str) else None
            if not text and isinstance(res, dict) and res.get("choices"):
                text = ((res["choices"][0].get("message") or {}).get("content")) or ""
            if text:
                return text, None
            errs.append("empty")
        except Exception as e:
            errs.append(str(e)[:80])
    return None, "; ".join(errs)


def _dossier(st_lab, baseline, extra, escalation=None):
    tail = []
    if os.path.exists(JOURNAL_PATH):
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            rows = f.readlines()[-14:]
        for r in rows:
            try:
                d = json.loads(r)
                tail.append(f"{d.get('event')}: {d.get('detail', '')[:120]}")
            except Exception:
                tail.append(r[:120])
    data_cfg = _main_cfg({})
    bounds = {k: [b[0], b[1]] for k, b in merged_bounds().items()}
    pats = _load_json(PATTERNS_PATH, {})
    ps = _load_json(PAPER_STATE_PATH, {})
    pchk = _load_json(PAPER_CHECK_PATH, {})
    evo = _load_json(os.path.join(RESULTS, "evo.json"), {})
    return json.dumps({
        "цель": "обогнать оригинал по score при PF>1 и глубине трейдов",
        "эскалация": escalation and {k: escalation.get(k)
                                     for k in ("kind", "prio", "detail")},
        "оригинал_baseline": {k: baseline.get(k) for k in
                              ("name", "score", "trades", "pnl") if k in baseline},
        "лаборатория_лучшая": st_lab.get("best"),
        "внедрено_в_оригинал": st_lab.get("promoted", [])[-4:],
        "паттерны_охоты": pats,
        "deep_scan": st_lab.get("deep_scan"),
        "lab_paper": ps and {"equity": ps.get("equity"), "stats": ps.get("stats"),
                             "open": len(ps.get("positions") or [])},
        "lab_paper_сверка": (pchk.get("summary") if isinstance(pchk, dict) else None),
        "evo": evo and {"generations": evo.get("generations"),
                        "best": evo.get("best")},
        "журнал_недавнее": tail,
        "параметры_сейчас": {k: data_cfg.get(k) for k in list(bounds)[:12]},
        "границы": bounds,
        "ml": _ml_snapshot(),
        "real_check": (extra or {}).get("real_check"),
        "universe": _eval_symbols(),
    }, ensure_ascii=False)


def _plan(st_lab, baseline, extra, deadline, escalation=None):
    st_lab["llm_used"] = (st_lab.get("llm_used", 0) + 1
                          if st_lab.get("llm_day") == _day_key() else 1)
    st_lab["llm_day"] = _day_key()
    st_lab["last_plan_ts"] = _now_ms()
    text, err = _llm_call(_dossier(st_lab, baseline, extra, escalation))
    st_lab["last_escalation"] = escalation and escalation.get("kind")
    if text is None:
        journal("plan_failed", detail=err or "llm недоступен")
        return False
    data = ai_advisor._parse_json(text)
    if not isinstance(data, dict):
        journal("plan_failed", detail="невалидный JSON от LLM")
        return False
    queue = []
    for i, v in enumerate((data.get("queue") or [])[:3]):
        if not isinstance(v, dict):
            continue
        params, rej = _clamp_params(v.get("params"))
        mod_entry = None
        mod = v.get("module")
        if isinstance(mod, dict) and isinstance(mod.get("code"), str):
            ns, merr = ai_dev.validate_module(mod["code"])
            mod_entry = {"name": ns["name"], "code": ns["code"]} if ns else {
                "name": str(mod.get("name", f"lab_mod_{i}")), "error": merr}
        queue.append({"name": str(v.get("name", f"exp_{i}"))[:40],
                      "params": params, "module": mod_entry,
                      "note": str(v.get("note", ""))[:200],
                      "rejected": rej})
    thoughts = str(data.get("thoughts", ""))[:200]
    for d in (data.get("data") or [])[:2]:
        if isinstance(d, dict) and str(d.get("symbol", ""))[:16]:
            journal("data_request", symbol=str(d.get("symbol"))[:16],
                    days=int(d.get("days", 10) or 10),
                    detail="скачано при следующем бэктесте")
    st_lab["queue"] = queue
    st_lab["q_idx"] = 0
    st_lab["phase"] = "evaluate"
    save_state(st_lab)
    for act in (data.get("run") or [])[:2]:
        _run_ai_action(str(act), st_lab, deadline)
    journal("plan", detail=thoughts, queued=len(queue),
            trigger=escalation and escalation.get("kind"))
    return True


# ------------------------------------------------------------ шаг лаборатории
def maybe_step(st, trades, cfg_main, extra=None, time_budget_s=STEP_TIME_S):
    deadline = time.time() + time_budget_s
    st_lab = load_state()
    summary = {"ok": False, "phase": st_lab.get("phase")}
    try:
        return _step(st_lab, deadline, summary, extra)
    except Exception as e:
        journal("step_crash", detail=f"{type(e).__name__}: {str(e)[:160]}")
        summary["error"] = str(e)[:120]
        _write_report(st_lab, summary)
        return summary


def _step(st_lab, deadline, summary, extra=None):
    extra = extra or {}
    if st_lab.get("llm_day") != _day_key():
        st_lab["llm_used"] = 0
        st_lab["llm_day"] = _day_key()

    # 0) живой лабораторный форвард-тест на реальных свечах этого цикла
    bars_map = extra.get("bars")
    if bars_map and time.time() < deadline - 45:
        opened = paper_tick(bars_map, st_lab, _now_ms(), deadline)
        if opened:
            summary["paper_opened"] = opened
        if (_now_ms() - int(st_lab.get("paper_last_check_ts", 0))
                > PAPER_CHECK_INTERVAL_S * 1000
                and time.time() < deadline - 60):
            rc = _paper_realcheck(_main_cfg({}), deadline)
            if rc:
                summary["paper_checked"] = rc.get("trades")

    if time.time() > deadline - 30:
        st_lab["steps"] = st_lab.get("steps", 0) + 1
        save_state(st_lab)
        _write_report(st_lab, summary)
        summary["ok"] = True
        summary["phase"] = st_lab["phase"]
        return summary

    if not st_lab.get("baseline") and time.time() < deadline - 40:
        base = ensure_baseline(deadline)
        if base:
            journal("baseline", score=base.get("score"),
                    pnl=base.get("pnl"), trades=base.get("trades"))
            st_lab["baseline"] = base
            st_lab["baseline_ts"] = _now_ms()
            st_lab["phase"] = "plan"
            summary["baseline"] = {k: base.get(k) for k in ("score", "trades", "pnl")}

    queue = st_lab.get("queue") or []
    q_idx = int(st_lab.get("q_idx", 0))
    llm_ok = (st_lab.get("llm_used", 0) < LLM_DAY_LIMIT - 1
              and _now_ms() - st_lab.get("last_plan_ts", 0) > PLAN_INTERVAL_S * 1000)

    pend = st_lab.get("pending")
    if pend and time.time() < deadline - 90:
        ck = _load_json(os.path.join(CKPT_DIR, f"{int(pend):03d}.json"), None)
        if ck:
            if (st_lab.get("deep_scan") or {}).get("verdict") == "fail":
                journal("promote_blocked", name=ck.get("name"),
                        detail="deep_scan: вариант не устойчив на длинной истории")
            elif _confirm(ck, deadline):
                _promote(st_lab, ck, {"module": ck.get("module")})
                journal("promoted", name=ck.get("name"),
                        params=ck.get("params"), score=ck.get("score"),
                        detail="подтверждено вторым окном предыдущего шага")
            else:
                journal("confirm_failed", name=ck.get("name"),
                        detail="второе окно не подтвердило, вариант отброшен")
        st_lab["pending"] = None

    trig = []
    if time.time() < deadline - 20:
        trig = _check_triggers(st_lab, extra)
    esc_budget = st_lab.get("llm_used", 0) < LLM_DAY_LIMIT
    critical = [t for t in trig if t["prio"] == "critical"]

    if critical and esc_budget and time.time() < deadline - 60:
        st_lab["phase"] = "ai_escalation"
        save_state(st_lab)
        done = _plan(st_lab, st_lab.get("baseline") or {}, extra, deadline,
                     escalation=critical[0])
        summary["escalation"] = critical[0]["kind"]
        summary["planned"] = done
    elif queue[q_idx:] and time.time() < deadline - 50:
        summary.update(_evaluate(st_lab, deadline))
    elif trig and esc_budget and time.time() < deadline - 60:
        st_lab["phase"] = "ai_escalation"
        save_state(st_lab)
        done = _plan(st_lab, st_lab.get("baseline") or {}, extra, deadline,
                     escalation=trig[0])
        summary["escalation"] = trig[0]["kind"]
        summary["planned"] = done
    elif llm_ok and time.time() < deadline - 60:
        st_lab["phase"] = "plan"
        save_state(st_lab)
        done = _plan(st_lab, st_lab.get("baseline") or {}, extra, deadline)
        summary["planned"] = done
    elif time.time() < deadline - 120 and (
            _now_ms() - int(st_lab.get("deep_ts", 0)) > DEEP_SCAN_INTERVAL_S * 1000):
        ds = _deep_scan(st_lab, deadline)
        if ds:
            st_lab["deep_ts"] = _now_ms()
            summary["deep_scan"] = ds.get("verdict")
        st_lab["phase"] = "deep_scan"
    elif time.time() < deadline - 100 and (
            _now_ms() - int(st_lab.get("hunt_ts", 0)) > HUNT_INTERVAL_S * 1000):
        st_lab["phase"] = "hunt"
        save_state(st_lab)
        _hunt(st_lab, deadline)
        summary["hunt"] = len(st_lab.get("queue") or [])
    else:
        st_lab["phase"] = "cooldown" if not llm_ok else st_lab["phase"]
        summary["idle"] = True
        summary["llm_used"] = st_lab.get("llm_used", 0)
    st_lab["steps"] = st_lab.get("steps", 0) + 1
    save_state(st_lab)
    _write_report(st_lab, summary)
    summary["ok"] = True
    summary["phase"] = st_lab["phase"]
    return summary


# --------------------------------------------------------------- evaluation
def _evaluate(st_lab, deadline):
    out = {"evaluated": 0}
    queue = st_lab.get("queue") or []
    q_idx = int(st_lab.get("q_idx", 0))
    base = st_lab.get("baseline")
    if not base:
        return out

    while q_idx < len(queue) and out["evaluated"] < MAX_VARIANTS_PER_STEP \
            and time.time() < deadline - 45:
        variant = queue[q_idx]
        if variant.get("module") and variant["module"].get("error"):
            journal("module_rejected", name=variant["module"].get("name"),
                    detail=variant["module"]["error"][:160])
            q_idx += 1
            continue
        symbols = _eval_symbols()
        cfg = _main_cfg(variant.get("params") or {})
        lab_strats.activate(cfg)
        if variant.get("module"):
            reg = _lab_registry()
            exists = {m["name"] for m in reg}
            if variant["module"]["name"] not in exists:
                path = os.path.join(DEV_DIR, f"lab_{variant['module']['name']}.py")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(variant["module"]["code"])
                reg.append({"name": variant["module"]["name"], "file": path,
                            "status": "active", "decisions": 0, "vetoes": 0,
                            "fails": 0, "origin": "lab"})
                _save_json(MODULES_REGISTRY, reg)
            _with_lab_modules()
        wins, _srcs = _cached_windows(symbols, EVAL_DAYS)
        st_sim, tr, _eq = sim_mod.replay(wins, cfg,
                                         step_ms=opt_mod.OPT_STEP_MS,
                                         symbols=symbols)
        _restore_main_modules()
        res = _pack(st_sim, tr, cfg, variant["name"])
        res["note"] = variant.get("note", "")
        res["module"] = (variant.get("module") or {}).get("name") \
            if variant.get("module") else None
        ok, why = _robust(res, base)
        ds_fail = (st_lab.get("deep_scan") or {}).get("verdict") == "fail"
        if ok:
            st_lab["best"] = res
            st_lab["ckpts"] = st_lab.get("ckpts", 0) + 1
            _save_json(os.path.join(CKPT_DIR, f"{st_lab['ckpts']:03d}.json"),
                       {"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                        **res})
            _prune_checkpoints()
            if ds_fail:
                st_lab["pending"] = st_lab["ckpts"]
                journal("promote_blocked", name=variant["name"],
                        detail="deep_scan: устойчивость не подтверждена, промоушен заморожен")
            elif time.time() < deadline - 60 and _confirm(res, deadline):
                _promote(st_lab, res, variant)
                journal("promoted", name=variant["name"],
                        params=res.get("params"), score=res.get("score"),
                        detail="второе окно подтвердило превосходство")
            elif time.time() < deadline - 60:
                journal("confirm_failed", name=variant["name"],
                        detail="второе окно не подтвердило, вариант отброшен")
            else:
                st_lab["pending"] = st_lab["ckpts"]
                journal("checkpoint", name=variant["name"], score=res.get("score"),
                        detail="лучше baseline, подтверждение перенесено на следующий шаг")
        else:
            journal("failed", name=variant["name"], detail=why[:160],
                    score=res.get("score"), pnl=res.get("pnl"))
        out["evaluated"] += 1
        q_idx += 1
    if q_idx >= len(queue):
        queue.clear()
        q_idx = 0
        st_lab["queue"], st_lab["q_idx"] = [], 0
    else:
        st_lab["q_idx"] = q_idx
    return out


_saved_modules_path = None
_saved_cache = None


def _with_lab_modules():
    """На время бэктеста лабы активны ЕЁ модули (lab/modules.json) —
    оригинальный реестр не трогаем."""
    global _saved_modules_path, _saved_cache
    _saved_modules_path = ai_dev.MODULES_PATH
    _saved_cache = dict(ai_dev._module_cache)
    ai_dev.MODULES_PATH = MODULES_REGISTRY
    ai_dev._module_cache = {"key": None, "mods": []}


def _restore_main_modules():
    global _saved_modules_path, _saved_cache
    if _saved_modules_path is not None:
        ai_dev.MODULES_PATH = _saved_modules_path
        ai_dev._module_cache = _saved_cache
        _saved_modules_path = None


def _robust(res, base):
    if res["score"] <= -1e8:
        return False, "недостаточно трейдов или противоречивые чанки"
    if res["pnl"] <= base["pnl"]:
        return False, f"pnl {res['pnl']} <= baseline {base['pnl']}"
    if base["score"] > -1e8 and res["score"] <= base["score"] * 1.02:
        return False, (f"score {res['score']} не выше baseline "
                       f"{base['score']} +2%")
    return True, ""


def _confirm(res, deadline):
    """Второе окно: более старый кусок истории. Только если есть время."""
    if time.time() > deadline - 60:
        return False
    symbols = _eval_symbols()
    cfg = _main_cfg({})
    for k, v in (res.get("params") or {}).items():
        if _is_bounded(k):
            cfg[k] = v
    wins, _srcs = _cached_windows(symbols, EVAL_DAYS + 6)
    ts_list = sorted(w.ts for w in wins.values()
                     if len(w.ts) > sim_mod.WARMUP_BARS)
    if len(ts_list) < 2:
        return False
    sym0 = symbols[0]
    w = wins[sym0]
    i = sim_mod.WARMUP_BARS + 6 * 24 * 60  # 6 дней назад от окна eval
    if i >= len(w.ts):
        return False
    end_old = w.ts[i]
    st_sim, tr, _eq = sim_mod.replay(wins, cfg, start_ts=end_old,
                                     step_ms=opt_mod.OPT_STEP_MS,
                                     symbols=symbols)
    res2 = _pack(st_sim, tr, cfg, "confirm")
    return res2["score"] > -1e8 and res2["pnl"] > 0


def _is_bounded(k):
    return k in merged_bounds()


# --------------------------------------------------------------- promotion
def _promote(st_lab, res, variant):
    params = {k: v for k, v in (res.get("params") or {}).items() if _is_bounded(k)}
    prev = _load_json(RECOMMEND_PATH, {})
    merged = dict(prev.get("params") or {})
    merged.update(params)
    _save_json(RECOMMEND_PATH, {
        "ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "params": merged,
        "robust": True,
        "source": "lab seconda realidad",
        "last_score": res.get("score"),
    })
    promoted = st_lab.setdefault("promoted", [])
    mod = variant.get("module")
    if mod and not mod.get("error"):
        reg_main = []
        mreg = os.path.join(RESULTS, "ai_modules.json")
        if os.path.exists(mreg):
            try:
                reg_main = json.load(open(mreg))
            except Exception:
                reg_main = []
        if not any(m.get("name") == f"lab_{mod['name']}" for m in reg_main):
            src_path = os.path.join(DEV_DIR, f"lab_{mod['name']}.py")
            if os.path.exists(src_path):
                reg_main.append({"name": f"lab_{mod['name']}",
                                 "file": src_path, "status": "active",
                                 "decisions": 0, "vetoes": 0, "fails": 0,
                                 "origin": "lab"})
                with open(mreg, "w", encoding="utf-8") as f:
                    json.dump(reg_main, f, ensure_ascii=False, indent=1)
                promoted.append({"module": f"lab_{mod['name']}"})
    promoted.append({"params": params, "score": res.get("score"),
                     "ts": time.strftime("%Y-%m-%d %H:%M", time.gmtime())})
    st_lab["promoted"] = promoted[-10:]


def load_recommend_overrides():
    data = _load_json(RECOMMEND_PATH, {})
    if data.get("robust") and data.get("params"):
        return data["params"]
    return {}


def _prune_checkpoints():
    if not os.path.isdir(CKPT_DIR):
        return
    files = sorted(os.listdir(CKPT_DIR))
    while len(files) > MAX_CHECKPOINTS:
        os.remove(os.path.join(CKPT_DIR, files.pop(0)))


def _lab_registry():
    return _load_json(MODULES_REGISTRY, [])


# ------------------------------------------------- охота за паттернами (без LLM)
VOL_CUTS = (0.7, 1.3, 2.5)
MOM_CUTS = (-0.01, 0.0, 0.01)
ATR_CUTS = (0.002, 0.008)


def _bar_tags(vol_spike, mom, atr_rel):
    if vol_spike < VOL_CUTS[0]:
        vz = "vz0"
    elif vol_spike < VOL_CUTS[1]:
        vz = "vz1"
    elif vol_spike < VOL_CUTS[2]:
        vz = "vz2"
    else:
        vz = "vz3"
    if mom <= MOM_CUTS[0]:
        mt = "m0"
    elif mom < MOM_CUTS[1]:
        mt = "m1"
    elif mom < MOM_CUTS[2]:
        mt = "m2"
    else:
        mt = "m3"
    if atr_rel < ATR_CUTS[0]:
        at = "a0"
    elif atr_rel < ATR_CUTS[1]:
        at = "a1"
    else:
        at = "a2"
    return vz, mt, at


def _pattern_module(pid, tags, side, mean_ret, n):
    """Вето-модуль по найденной контр-закономерности (детерминированный)."""
    tag_list = ", ".join(f'"{t}"' for t in tags)
    return f'''"""Лаб-модуль: вето по опасному контексту {pid} (охота без LLM).
math/statistics в песочнице уже доступны как глобалы."""
NAME = "lab_pat_{pid}"
SIDE_BLOCK = "{side}"
BLACK = {{{tag_list}}}


def _vol_tag(vs):
    if vs < 0.7:
        return "vz0"
    if vs < 1.3:
        return "vz1"
    if vs < 2.5:
        return "vz2"
    return "vz3"


def _mom_tag(feat):
    c = feat.get("close") or 0.0
    m = (feat.get("momentum") or 0.0) / (c if abs(c) > 1e-9 else 1.0)
    if m <= -0.01:
        return "m0"
    if m < 0.0:
        return "m1"
    if m < 0.01:
        return "m2"
    return "m3"


def _atr_tag(feat):
    c = feat.get("close") or 0.0
    a = feat.get("atr15") or 0.0
    at = a / (c if abs(c) > 1e-9 else 1.0)
    if at < 0.002:
        return "a0"
    if at < 0.008:
        return "a1"
    return "a2"


def filter_signal(feat, cfg):
    tags = {{_vol_tag(feat.get("vol_spike") or 0.0), _mom_tag(feat), _atr_tag(feat)}}
    if BLACK <= tags:
        return False, "lab pattern: {pid}"
    return True, "ok"
'''


def _hunt(st_lab, deadline):
    """Детерминированная добыча: теги контекста -> форвард-исход -> эдж/чёрные
    паттерны. Без LLM; результат = patterns.json + очередь pack-вариантов и
    вето-модулей (проверяются тем же robust-гейтом)."""
    symbols = _eval_symbols()
    wins, _srcs = _cached_windows(symbols, EVAL_DAYS)
    out_stats = {}
    for sym, w in wins.items():
        bars = w.bars
        closes = [b["c"] for b in bars]
        n_bars = len(bars)
        if n_bars < 300 + HUNT_FWD_BARS:
            continue
        idx0 = max(240, n_bars - 4000)
        for i in range(idx0, n_bars - HUNT_FWD_BARS):
            vols = [b["v"] for b in bars[max(0, i - 20):i]]
            vavg = sum(vols) / len(vols) if vols else 0.0
            spike = bars[i]["v"] / vavg if vavg > 0 else 0.0
            c0 = closes[i]
            mom = (c0 - closes[max(0, i - 10)]) / (c0 if abs(c0) > 1e-9 else 1.0)
            atr_rel = (bars[i]["h"] - bars[i]["l"]) / (c0 if abs(c0) > 1e-9 else 1.0)
            tags = _bar_tags(spike, mom, atr_rel)
            fwd = closes[i + HUNT_FWD_BARS] / c0 - 1.0
            for key in ((tags[0],), (tags[1],), (tags[2],),
                        (tags[0], tags[1]), (tags[0], tags[2]), (tags[1], tags[2])):
                rec = out_stats.setdefault(key, [0, []])
                rec[0] += 1
                ci = i * 4 // n_bars
                while len(rec[1]) < 4:
                    rec[1].append([])
                rec[1][ci].append(fwd)
            if time.time() > deadline - 30:
                break
        if time.time() > deadline - 30:
            break
    patterns, blacks = [], []
    for key, rec in out_stats.items():
        n = rec[0]
        if n < HUNT_MIN_BARS or len([ch for ch in rec[1] if ch]) < 4:
            continue
        mean = sum(sum(ch) for ch in rec[1]) / n
        chunks = [sum(ch) / len(ch) for ch in rec[1] if ch]
        sign = mean > 0
        consistent = sum(1 for ch in chunks if (ch > 0) == sign)
        if consistent < 3 or abs(mean) < HUNT_MIN_EDGE:
            continue
        pid = "_".join(key)
        info = {"id": pid, "tags": list(key), "side": "long" if sign else "short",
                "mean_ret": round(mean, 5), "n": n, "chunks_ok": f"{consistent}/4"}
        patterns.append(info)
        blacks.append((abs(mean), info, "long" if not sign else "short"))
    patterns.sort(key=lambda p: p["mean_ret"], reverse=True)
    _save_json(PATTERNS_PATH, {"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                              "patterns": patterns[:12], "contexts": len(out_stats)})
    blacks.sort(key=lambda b: (-b[0], -b[1]["n"]))
    queue = [{"name": "pack_scalp", "params": {"s_scalp": 1, "ttl_min": 180, "ttl_max": 300},
              "note": "охота: pack скальпинга на спокойном рынке"},
             {"name": "pack_pump_fade", "params": {"s_pump_fade": 1},
              "note": "охота: фейд пампов/дампов на аномальном объёме"},
             {"name": "pack_squeeze_break", "params": {"s_squeeze_break": 1},
              "note": "охота: пробой сжатого Donchian-канала"}]
    for _strength, rec, block_side in blacks[:2]:
        code = _pattern_module(rec["id"], rec["tags"], block_side,
                               rec["mean_ret"], rec["n"])
        ns, err = ai_dev.validate_module(code)
        if ns:
            queue.append({"name": f"pat_veto_{rec['id']}", "params": {},
                          "module": {"name": ns["name"], "code": code},
                          "note": f"вето: контекст {rec['id']} систематически бьёт {block_side} ({rec['mean_ret']})"})
        else:
            journal("hunt_module_rejected", detail=err[:120])
    if blacks:
        b0 = blacks[0]
        code = _pattern_module(b0[1]["id"], b0[1]["tags"], b0[2],
                               b0[1]["mean_ret"], b0[1]["n"])
        queue.append({"name": f"combo_pat_{b0[1]['id']}",
                      "params": {"s_scalp": 1, "ttl_min": 240, "ttl_max": 420},
                      "module": {"name": f"lab_pat_{b0[1]['id']}", "code": code},
                      "note": "охота: скальпинг + вето худшего контекста"})
    st_lab["hunt_ts"] = _now_ms()
    st_lab["queue"] = queue
    st_lab["q_idx"] = 0
    st_lab["phase"] = "evaluate"
    journal("hunt", patterns=len(patterns), blacks=len(blacks),
            queued=len(queue),
            detail=("топ: " + patterns[0]["id"] + " " + str(patterns[0]["mean_ret"]))
            if patterns else "чисто — закономерностей выше порога нет")


def _deep_scan(st_lab, deadline):
    """Прогон устойчивости: DEEP_SCAN_DAYS дней на 15m, baseline vs best.
    verdict=fail блокирует дальнейшие промоушены до следующего скана."""
    if time.time() > deadline - 120:
        return None
    symbols = _eval_symbols()
    try:
        wins, _srcs = _cached_windows(symbols, DEEP_SCAN_DAYS, tf=DEEP_SCAN_TF)
        if time.time() > deadline - 100:
            return None
        base_cfg = _main_cfg({})
        st1, tr1, _eq = sim_mod.replay(wins, base_cfg, step_ms=30 * 60_000,
                                       symbols=symbols)
        res_b = _pack(st1, tr1, base_cfg, "deep_baseline")
        res_best = None
        best = st_lab.get("best")
        if best and best.get("params"):
            bcfg = _main_cfg(best.get("params"))
            lab_strats.activate(bcfg)
            st2, tr2, _eq = sim_mod.replay(wins, bcfg, step_ms=30 * 60_000,
                                           symbols=symbols)
            _restore_main_modules()
            res_best = _pack(st2, tr2, bcfg, "deep_best")
        verdict = "pass"
        if res_best is not None:
            verdict = ("pass" if res_best["score"] > -1e8
                       and res_best["pnl"] > min(0.0, res_b["pnl"]) else "fail")
        st_lab["deep_scan"] = {"ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                               "days": DEEP_SCAN_DAYS, "tf": DEEP_SCAN_TF,
                               "score_base": res_b["score"],
                               "pnl_base": res_b["pnl"],
                               "trades_base": res_b["trades"],
                               "score_best": res_best and res_best["score"],
                               "pnl_best": res_best and res_best["pnl"],
                               "trades_best": res_best and res_best["trades"],
                               "verdict": verdict}
        journal("deep_scan", verdict=verdict, days=DEEP_SCAN_DAYS,
                tf=DEEP_SCAN_TF, score_base=res_b["score"],
                trades_base=res_b["trades"],
                score_best=res_best and res_best["score"],
                trades_best=res_best and res_best["trades"])
        return st_lab["deep_scan"]
    except Exception as e:
        journal("deep_scan_error", detail=f"{type(e).__name__}: {str(e)[:160]}")
        return None


# ---------------------------------------------------- лабораторная paper-торговля
def _load_paper_trades():
    if not os.path.exists(PAPER_TRADES_PATH):
        return []
    try:
        with open(PAPER_TRADES_PATH, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception:
        return []


def paper_tick(bars_map, st_lab, now_ms, deadline):
    """Живой форвард-тест в лабе: лучшая её конфигурация торгует на реальных
    свечах текущего цикла — своё состояние, свои сделки, своя кривая."""
    if not bars_map:
        return 0
    pstate = _load_json(PAPER_STATE_PATH, None)
    if pstate is None:
        pstate = sim_mod.fresh_state()
    for k, v in sim_mod.fresh_state().items():
        pstate.setdefault(k, v)
    trades = _load_paper_trades()
    best = st_lab.get("best") or {}
    pcfg = _main_cfg(best.get("params") or {})
    if pstate.get("day_start_equity") is None:
        pstate["day_start_equity"] = pstate.get("equity") or 1000.0
    opened = 0
    try:
        engine.resolve_positions(pstate, trades, bars_map, pcfg, now_ms)
        engine.roll_day(pstate, now_ms)
        ok, _why = engine.breakers_ok(pstate, pcfg, now_ms)
        if ok and time.time() < deadline - 20:
            lab_strats.activate(pcfg)
            _with_lab_modules()
            try:
                for sym, bars in bars_map.items():
                    if len(pstate["positions"]) >= pcfg.get("max_concurrent", 3):
                        break
                    if any(p["symbol"] == sym for p in pstate["positions"]):
                        continue
                    if not bars or now_ms - bars[-1]["ts"] > 5 * 60_000:
                        continue
                    lb = bars[:-1] if (now_ms - bars[-1]["ts"] < 60_000
                                       and len(bars) > 60) else bars
                    feat = features.features(lb, pcfg)
                    if feat is None:
                        continue
                    decision = strategies.decide(feat, pcfg, pstate.get("adapt"))
                    if not decision:
                        continue
                    allow, _why2 = ai_dev.filter_decision(feat, pcfg, decision)
                    if not allow:
                        continue
                    side, strat, reg = decision
                    pos = engine.open_positions(pstate, pcfg, sym, feat, side,
                                                now_ms, "lab-paper",
                                                strategy=strat, regime=reg)
                    if pos:
                        opened += 1
                        journal("paper_open", symbol=sym, side=side,
                                strategy=strat,
                                detail=f"equity {round(pstate['equity'], 2)}")
            finally:
                _restore_main_modules()
    except Exception as e:
        journal("paper_error", detail=f"{type(e).__name__}: {str(e)[:160]}")
        return 0
    with open(PAPER_TRADES_PATH, "w", encoding="utf-8") as f:
        for t in trades[-400:]:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    if not os.path.exists(PAPER_EQ_PATH):
        with open(PAPER_EQ_PATH, "w", encoding="utf-8") as f:
            f.write("ts,equity\n")
    with open(PAPER_EQ_PATH, "a", encoding="utf-8") as f:
        f.write(f"{now_ms},{pstate['equity']:.4f}\n")
    pstate["updated"] = now_ms
    _save_json(PAPER_STATE_PATH, pstate)
    return opened


def _paper_realcheck(cfg, deadline):
    """Часовая реальная сверка закрытых лабораторных сделок: те же реальные
    свечи, комиссии и проскальзывание, что у основного движка."""
    if time.time() > deadline - 40:
        return None
    saved_path = verify.CHECK_PATH
    try:
        verify.CHECK_PATH = PAPER_CHECK_PATH
        rows = verify._load_rows()
        ids = {o.get("id") for o in rows}
        trades = _load_paper_trades()
        checked = 0
        for t in reversed(trades):
            if checked >= 2 or time.time() > deadline - 20:
                break
            if t.get("id") in ids:
                continue
            if t.get("status") not in ("win", "loss", "timeout"):
                continue
            end = t.get("closed_ts") or t.get("expires_ts")
            if not end:
                continue
            try:
                bars = verify._bars(t["symbol"], t["opened_ts"],
                                    min(end + 60_000,
                                        t["opened_ts"] + 3 * 3600_000))
                kind, pnl_real, amb = verify._real_replay(
                    t, bars, cfg, cfg.get("slip_entry_bps", 1.0),
                    cfg.get("slip_stop_bps", 2.0))
            except Exception as e:
                journal("paper_realcheck_error", symbol=t.get("symbol"),
                        detail=str(e)[:100])
                continue
            rows.append({"id": t.get("id"), "symbol": t.get("symbol"),
                         "side": t.get("side"),
                         "opened": time.strftime("%m-%d %H:%M",
                                                 time.gmtime(t["opened_ts"] / 1000)),
                         "status_recorded": t.get("status"), "kind_real": kind,
                         "pnl_recorded": t.get("pnl"), "pnl_real": pnl_real,
                         "ambiguous": amb,
                         "match": kind == t.get("status")})
            checked += 1
            ids.add(t.get("id"))
            time.sleep(0.15)
        rows = rows[-300:]
        summary = verify._summary(rows, [])
        with open(PAPER_CHECK_PATH, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "trades": rows}, f,
                      ensure_ascii=False, indent=1)
        if checked:
            journal("paper_realcheck", checked=checked,
                    match_rate=summary.get("match_rate"),
                    detail=f"lab-paper real_total {summary.get('real_total')}")
        st_lab = load_state()
        st_lab["paper_last_check_ts"] = _now_ms()
        save_state(st_lab)
        return summary if checked else None
    finally:
        verify.CHECK_PATH = saved_path


# ----------------------------------------------------------------- report
def _write_report(st_lab, summary):
    rep = {
        "ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "phase": st_lab.get("phase"),
        "steps": st_lab.get("steps", 0),
        "llm_used_today": st_lab.get("llm_used", 0),
        "llm_day_limit": LLM_DAY_LIMIT,
        "queue_left": max(len(st_lab.get("queue") or []) - int(st_lab.get("q_idx", 0)), 0),
        "checkpoints": st_lab.get("ckpts", 0),
        "baseline": st_lab.get("baseline") and {
            k: st_lab["baseline"].get(k) for k in ("score", "trades", "wins", "pnl")},
        "best": st_lab.get("best") and {
            k: st_lab["best"].get(k) for k in ("name", "score", "trades", "wins", "pnl", "note")},
        "promoted": (st_lab.get("promoted") or [])[-3:],
        "last": {k: summary.get(k) for k in ("evaluated", "planned", "idle", "error", "hunt", "deep_scan", "paper_opened")
                 if k in summary},
    }
    pats = _load_json(PATTERNS_PATH, {})
    rep["patterns"] = {"count": len(pats.get("patterns") or []),
                       "ts": pats.get("ts"),
                       "top": (pats.get("patterns") or [])[:3]}
    rep["deep_scan"] = st_lab.get("deep_scan") and {
        k: st_lab["deep_scan"].get(k) for k in
        ("ts", "verdict", "days", "tf", "score_base", "score_best",
         "trades_base", "trades_best")}
    ps = _load_json(PAPER_STATE_PATH, {})
    if ps:
        s = ps.get("stats") or {}
        rep["paper"] = {"equity": round(ps.get("equity") or 0, 2),
                        "trades": s.get("trades", 0),
                        "wins": s.get("wins", 0),
                        "pnl": round(s.get("pnl", 0.0), 2),
                        "open": len(ps.get("positions") or []),
                        "active_pack": (st_lab.get("best") or {}).get("name")}
    pchk = _load_json(PAPER_CHECK_PATH, {}).get("summary")
    if pchk:
        rep["paper_realcheck"] = {k: pchk.get(k) for k in
                                  ("updated", "trades", "real_total",
                                   "recorded_total", "avg_bias")}
    if st_lab.get("last_escalation"):
        rep["escalation"] = {"kind": st_lab.get("last_escalation"),
                             "llm_used": st_lab.get("llm_used", 0)}
    _save_json(REPORT_PATH, rep)
    return rep


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="AEGIS лаборатория (вторая реальность)")
    ap.add_argument("--run", action="store_true", help="один бюджетный шаг")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rollback", type=int, metavar="N",
                    help="откатить лучшее к чекпоинту N")
    ap.add_argument("--budget-s", type=int, default=STEP_TIME_S)
    args = ap.parse_args()
    if args.status:
        print(json.dumps(_load_json(REPORT_PATH, {}), ensure_ascii=False, indent=1))
        return
    if args.rollback is not None:
        path = os.path.join(CKPT_DIR, f"{args.rollback:03d}.json")
        ck = _load_json(path, None)
        st_lab = load_state()
        if ck:
            st_lab["best"] = ck
            save_state(st_lab)
            journal("rollback", detail=f"лучшая точка = чекпоинт {args.rollback}")
            print("rollback ok")
        else:
            print("checkpoint not found")
        return
    st = engine.load_state({})
    trs = engine.load_trades()
    _ = trs
    summary = maybe_step(st, trs, json.load(open(os.path.join(HERE, "config", "settings.json"))), {})
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
