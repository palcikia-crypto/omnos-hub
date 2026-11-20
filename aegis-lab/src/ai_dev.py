"""AEGIS AI-архитектор ("бог-режим"): Workers AI осматривает всю систему,
ставит бэктест-эксперименты в песочнице, пишет собственные модули-фильтры
сигналов и внедряет их после строгой проверки.

Границы безопасности:
- параметры только из DEV_BOUNDS с жёсткими границами;
- эксперимент проходит в симуляторе на истории (paper, реальные деньги
  заблокированы на уровне движка);
- модуль проходит: запрет импортов -> compile -> exec в изоляции ->
  smoke-тест на мок-фичах -> лимит размера; авто-отключение после 2 ошибок
  или если глушит >70% сигналов;
- модуль умеет только ВЕТО (усилить поток сигналов он не может).
"""
import ast
import json
import os
import re
import time

from src import engine
from src.ai_advisor import (
    WHITELIST, _ai_run, _parse_json, load_ai_overrides, API, MODELS,
)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = engine.RESULTS
DEV_DIR = os.path.join(HERE, "ai_modules")
MODULES_PATH = os.path.join(RESULTS, "ai_modules.json")
REPORT_PATH = os.path.join(RESULTS, "ai_dev_report.json")
JOURNAL_PATH = os.path.join(RESULTS, "ai_dev_journal.jsonl")

MIN_INTERVAL_S = 3600
RETRY_INTERVAL_S = 1200
MAX_MODULES = 3
MAX_LINES = 150
MAX_CHARS = 8000
VETO_RATE_RETIRE = 0.70
VETO_MIN_DECISIONS = 20

DEV_BOUNDS = {
    **WHITELIST,
    "atr_k": (0.4, 1.6, float),
    "rr": (1.0, 2.5, float),
    "vol_spike_min": (1.1, 2.0, float),
    "spread_buffer": (0.0001, 0.001, float),
}

SAFE_BUILTINS = {n: v for n, v in {
    "abs": abs, "min": min, "max": max, "len": len, "round": round,
    "float": float, "int": int, "bool": bool, "sum": sum, "str": str,
    "enumerate": enumerate, "range": range, "sorted": sorted, "zip": zip,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
}.items()}

BANNED_RE = re.compile(
    r"__import__|__builtins__|__class__|__globals__|__subclasses__|"
    r"\beval\s*\(|\bexec\s*\(|\bopen\s*\(|\bgetattr\s*\(|\bsetattr\s*\(|"
    r"\bcompile\s*\(|\bglobals\s*\(\s*\)|\bvars\s*\(|\bgetattr\b|"
    r"\bimport\s+(os|sys|subprocess|socket|urllib|requests|ctypes|shutil|"
    r"pathlib|importlib|threading|multiprocessing|pickle|base64|builtins)\b|"
    r"\bfrom\s+(os|sys|subprocess|socket|urllib|requests|ctypes|shutil|"
    r"pathlib|importlib|threading|multiprocessing|pickle|base64|builtins)\b")

SYSTEM = (
    "Ты AI-АРХИТЕКТОР торговой системы AEGIS-Lab (paper-only, реальные деньги заблокированы). "
    "Каждый час тебе дают полный досье системы: структуру кода, сигнатуры функций, конфиг, "
    "результаты (panpaper-трейды, симулятор, оптимизатор WFO, ML-гейт, адаптивный мозг). "
    "Твоя работа как живого разработчика: 1) осмыслить систему; 2) поставить эксперименты "
    "(бэктесты в песочнице на истории) с вариантами параметров; 3) при необходимости "
    "написать СВОЙ модуль-фильтр сигналов (python) и внедрить его; 4) точечно поправить параметры. "
    "Философия: безопасность и осторожность. Лучше меньше сделок, но с устойчивым эджем (Fee-Edge). "
    "Отвечай СТРОГО валидным JSON без markdown, по схеме:\n"
    '{"thoughts": "что ты осмотрел и понял, что будешь делать (по-русски, до 150 слов)", '
    '"experiments": [{"name": "короткое имя", "params": {"имя_параметра": число}}], '
    '"param_actions": [{"param": "имя", "value": число, "reason": "кратко"}], '
    '"module": null или {"name": "snake_case_имя", "purpose": "что фильтрует и зачем", '
    '"code": "полный python-код модуля"}\n'
    "Правила: имена параметров только из списка допустимых (дам ниже в досье с границами); "
    "экспериментов <= 3; каждый эксперимент - вариация 1-3 параметров. "
    "Модуль: код <= 150 строк, только import math/statistics, обязательные экспорты "
    "NAME (str) и filter_signal(feat, cfg) -> (allow: bool, reason: str), "
    "где allow=False означает ВЕТО на сделку (блокируй только явно рискованные сигналы, "
    "НЕ глуши всё подряд). "
    "feat - словарь с ключами: close, ema_fast, ema_slow, momentum, vol_spike, atr15, rsi14, "
    "bb_z, don_hi, don_lo, adx15, atr_pct (любые могут быть None). "
    "Подавай идеи МАКСИМАЛЬНО конструктивно: если торговля в плюсе - помоги сохранить эдж; "
    "если в минусе - ищи причину в данных и фильтрах (не повышай риск!)." )


def _ask_llm(user_text, max_tokens=2200):
    tok = os.environ.get("WAI_TOKEN")
    acc = os.environ.get("WAI_ACCOUNT")
    if not tok or not acc:
        return None, "no WAI_TOKEN/WAI_ACCOUNT"
    body = {"messages": [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user_text},
    ], "max_tokens": max_tokens, "temperature": 0.4}
    last_err = None
    for m in MODELS:
        try:
            r = _ai_run(acc, tok, m, body)
            if not r.get("success"):
                last_err = r.get("errors")
                continue
            res = r.get("result") or {}
            text = res.get("response") if isinstance(res.get("response"), str) else None
            if not text and isinstance(res, dict) and res.get("choices"):
                text = ((res["choices"][0].get("message") or {}).get("content")) or ""
            if text:
                return {"model": m, "text": text}, None
            last_err = "empty response"
        except Exception as e:
            last_err = str(e)
    return None, str(last_err)


def _file_dossier():
    """Структура системы: файлы, строки, докстринги, сигнатуры функций."""
    lines = []
    src_dir = os.path.join(HERE, "src")
    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".py") and f != "__init__.py")
    files.append("config/settings.json")
    for fn in files:
        path = os.path.join(src_dir, fn)
        if not os.path.exists(path):
            path = os.path.join(HERE, "config", "settings.json")
        with open(path) as f:
            src = f.read()
        n = len(src.splitlines())
        doc = ""
        m = re.match(r'"""([\s\S]+?)"""', src)
        if m:
            doc = m.group(1).replace("\n", " ")[:200]
        lines.append(f"== {fn} ({n} строк) ==\n{doc}")
        if fn.endswith(".py"):
            try:
                tree = ast.parse(src)
                sigs = []
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef,)):
                        try:
                            a = ast.unparse(node.args)[:160]
                        except Exception:
                            a = "..."
                        d = ast.get_docstring(node)
                        d = d.replace("\n", " ")[:110] if d else ""
                        sigs.append(f"def {node.name}({a}) // {d}")
                lines.append("\n".join(sigs[:14]))
            except Exception:
                pass
        else:
            lines.append(src[:1200])
    return "\n".join(lines)


def _state_digest(st, trades, cfg, extra):
    s = st["stats"]
    last = [{"sym": t.get("symbol"), "side": t.get("side"), "pnl": round(t.get("pnl", 0), 3),
             "closed": t.get("closed_reason", t.get("reason", ""))} for t in trades[-12:]]
    sim_st = None
    sim_path = os.path.join(RESULTS, "sim_state.json")
    if os.path.exists(sim_path):
        try:
            with open(sim_path) as f:
                sim_st = json.load(f)
        except Exception:
            pass
    opt = None
    opt_path = os.path.join(RESULTS, "opt.json")
    if os.path.exists(opt_path):
        try:
            with open(opt_path) as f:
                opt = json.load(f)
        except Exception:
            pass
    lb = []
    if opt:
        for r in opt.get("leaderboard", [])[:3]:
            lb.append({"params": r.get("params"), "score": r.get("score"),
                       "pnl": r.get("pnl"), "trades": r.get("trades")})
    advisor = {}
    rp = os.path.join(RESULTS, "ai_report.json")
    if os.path.exists(rp):
        try:
            with open(rp) as f:
                r = json.load(f)
            advisor = {"verdict": r.get("verdict"), "summary": r.get("summary", "")[:200]}
        except Exception:
            pass
    bounds = {k: [b[0], b[1]] for k, b in DEV_BOUNDS.items()}
    return {
        "timezone_note": "paper-only, cycle 5 min, TTL 300-900s",
        "live": {
            "equity": round(st["equity"], 2),
            "day_pnl": round(st["equity"] - st.get("day_start_equity", st["equity"]), 2),
            "consec_losses": st.get("consec_losses", 0),
            "stats": {k: s.get(k) for k in ("trades", "wins", "losses", "pnl")},
            "open_positions": len(st["positions"]),
            "last_trades": last,
        },
        "sim_sandbox": {
            "equity": round((sim_st or {}).get("equity", 0), 2),
            "stats": {k: (sim_st or {}).get("stats", {}).get(k) for k in ("trades", "wins", "losses", "pnl")},
        },
        "optimizer": lb,
        "ml": extra.get("ml"),
        "ml_gated_last_cycle": extra.get("ml_gated"),
        "real_check": extra.get("real_check"),
        "adapt_top": sorted(
            ({"combo": k, "ewma": round(v.get("ewma", 0), 3), "n": v.get("n", 0)}
             for k, v in (st.get("adapt") or {}).items()),
            key=lambda x: -x["ewma"])[:6],
        "params_now": {k: cfg.get(k) for k in DEV_BOUNDS},
        "allowed_params_bounds": bounds,
        "advisor_report": advisor,
    }


# --------------------------------------------------------------- modules

def _load_registry():
    if not os.path.exists(MODULES_PATH):
        return []
    try:
        with open(MODULES_PATH) as f:
            reg = json.load(f)
        return reg if isinstance(reg, list) else []
    except Exception:
        return []


def _save_registry(reg):
    os.makedirs(RESULTS, exist_ok=True)
    with open(MODULES_PATH, "w") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)


def _mock_feats():
    base = {"close": 65000.0, "ema_fast": 65001.0, "ema_slow": 64995.0, "momentum": 12.5,
            "vol_spike": 1.6, "atr15": 45.0, "rsi14": 58.0, "bb_z": 1.1,
            "don_hi": 65100.0, "don_lo": 64400.0, "adx15": 27.0, "atr_pct": 0.6, "ts": 0}
    bear = dict(base, ema_fast=64990.0, ema_slow=65010.0, momentum=-18.0, rsi14=38.0,
                vol_spike=1.9, bb_z=-1.8)
    odd = dict(base, atr15=None, rsi14=None, bb_z=None)
    return [dict(base), bear, odd]


def validate_module(code):
    """Валидация в песочнице. Возвращает (namespace, error)."""
    if not code or not isinstance(code, str):
        return None, "пустой код"
    if len(code) > MAX_CHARS:
        return None, f"код больше {MAX_CHARS} символов"
    if len(code.splitlines()) > MAX_LINES:
        return None, f"код больше {MAX_LINES} строк"
    if BANNED_RE.search(code):
        return None, "запрещённые конструкции (нужны только import math/statistics)"
    try:
        compiled = compile(code, "<ai-module>", "exec")
    except SyntaxError as e:
        return None, f"синтаксис: {e}"
    ns = {"__builtins__": SAFE_BUILTINS, "math": _math_mod(), "statistics": _stat_mod()}
    try:
        exec(compiled, ns)
    except Exception as e:
        return None, f"не исполняется: {e}"
    name = ns.get("NAME")
    fn = ns.get("filter_signal")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,30}", name or ""):
        return None, "нет NAME (str, snake_case)"
    if not callable(fn):
        return None, "нет функции filter_signal(feat, cfg)"
    try:
        for feat in _mock_feats():
            t0 = time.time()
            out = fn(feat, {"fee_edge_mult": 3.0, "atr_k": 0.8, "rr": 1.5,
                            "vol_spike_min": 1.4, "rsi_os": 30, "rsi_ob": 70,
                            "min_atr_frac": 0.0015, "fee_rate": 0.00075})
            dt = time.time() - t0
            ok = out[0] if isinstance(out, (tuple, list)) and len(out) >= 1 else False
            if not isinstance(ok, bool):
                return None, "filter_signal должен вернуть (allow: bool, reason: str)"
            if dt > 0.3:
                return None, f"слишком медленно ({dt:.2f}s)"
    except Exception as e:
        return None, f"smoke-тест: {type(e).__name__}: {e}"
    return {"name": name, "code": code, "ns": ns}, None


def _math_mod():
    import math
    return math


def _stat_mod():
    import statistics
    return statistics


_module_cache = {"key": None, "mods": []}


def _active_modules():
    """Возвращает (registry, mods): реестр загружен один раз и мутируется
    прямо в объектах entry — filter_decision сохраняет его на диск."""
    key = None
    if os.path.exists(MODULES_PATH):
        try:
            key = os.path.getmtime(MODULES_PATH)
        except OSError:
            key = None
    if key == _module_cache.get("key") and _module_cache.get("mods") is not None:
        return _module_cache["reg"], _module_cache["mods"]
    reg = _load_registry()
    mods = []
    for entry in reg:
        if entry.get("status") != "active":
            continue
        path = os.path.join(HERE, entry.get("file", ""))
        if not os.path.exists(path):
            path = os.path.join(DEV_DIR, os.path.basename(entry.get("file", "")))
        if not os.path.exists(path):
            entry["status"] = "failed"
            entry["fail_reason"] = "файл модуля пропал"
            continue
        try:
            with open(path) as f:
                code = f.read()
            compiled = compile(code, entry.get("name", "m"), "exec")
            import math
            import statistics
            ns = {"__builtins__": SAFE_BUILTINS, "math": math, "statistics": statistics}
            exec(compiled, ns)
            if callable(ns.get("filter_signal")):
                mods.append({"entry": entry, "fn": ns["filter_signal"]})
        except Exception:
            entry["status"] = "failed"
            entry["fail_reason"] = "не загружается в песочнице"
    _module_cache["key"] = key
    _module_cache["reg"] = reg
    _module_cache["mods"] = mods
    return reg, mods


def filter_decision(feat, cfg, decision, persist_every=10):
    """Хук перед open_positions: активные модули могут наложить ВЕТО.
    Никогда не падает: любой сбой = сигнал разрешён (fail-open)."""
    try:
        reg, mods = _active_modules()
        if not mods:
            return True, None
        dirty = False
        for m in mods:
            entry = m["entry"]
            if entry.get("status") != "active":
                continue
            name = entry.get("name", "?")
            try:
                out = m["fn"](feat, dict(cfg))
                allow = bool(out[0]) if isinstance(out, (tuple, list)) and out else True
                entry["decisions"] = entry.get("decisions", 0) + 1
                dirty = True
                if not allow:
                    entry["vetoes"] = entry.get("vetoes", 0) + 1
                    if (entry["decisions"] >= VETO_MIN_DECISIONS
                            and entry["vetoes"] >= VETO_RATE_RETIRE * entry["decisions"]):
                        entry["status"] = "retired"
                        entry["fail_reason"] = "глушит >70% сигналов, авто-отключён"
                        _save_registry(reg)
                        continue
                    _save_registry(reg)
                    dirty = False
                    return False, f"{name}: {str(out[1] if isinstance(out, (tuple, list)) and len(out) > 1 else '')[:80]}"
            except Exception:
                entry["fails"] = entry.get("fails", 0) + 1
                dirty = True
                if entry["fails"] >= 2:
                    entry["status"] = "failed"
                    entry["fail_reason"] = "ошибка в рантайме, авто-отключён"
                    _save_registry(reg)
        if dirty:
            try:
                _module_cache["persist"] = _module_cache.get("persist", 0) + 1
                if _module_cache["persist"] % persist_every == 0:
                    _save_registry(reg)
            except Exception:
                pass
        return True, None
    except Exception:
        return True, None


# ----------------------------------------------------------- experiments

def run_experiments(variants, cfg_base, days=7, max_symbols=4):
    """Бэктесты в песочнице: базовый cfg + варианты от AI."""
    from src import sim as sim_mod
    from src import optimize as opt_mod
    symbols = cfg_base.get("symbols") or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    symbols = symbols[:max_symbols]
    wins, _srcs = sim_mod.fetch_windows(days, symbols)
    rows = []
    baseline_cfg = dict(cfg_base)
    st, trades, _eq = sim_mod.replay(wins, baseline_cfg, step_ms=opt_mod.OPT_STEP_MS)
    base_score = opt_mod.combo_score(trades, st)
    rows.append({"name": "baseline (текущий)", "params": {}, "score": base_score,
                 "trades": st["stats"]["trades"],
                 "wr": round(100 * st["stats"]["wins"] / max(st["stats"]["trades"], 1), 1),
                 "pnl": round(st["stats"]["pnl"], 2)})
    for v in variants[:3]:
        if not isinstance(v, dict):
            continue
        override = {}
        for k, val in (v.get("params") or {}).items():
            if k not in DEV_BOUNDS or val is None:
                continue
            lo, hi, cast = DEV_BOUNDS[k]
            try:
                override[k] = max(lo, min(hi, cast(float(val))))
            except (TypeError, ValueError):
                continue
        if not override:
            continue
        vcfg = dict(cfg_base)
        vcfg.update(override)
        try:
            st, trades, _eq = sim_mod.replay(wins, vcfg, step_ms=opt_mod.OPT_STEP_MS)
        except Exception as e:
            rows.append({"name": v.get("name") or "вариант", "params": override,
                         "error": str(e)[:120], "score": -1e9, "trades": 0, "wr": 0, "pnl": 0})
            continue
        rows.append({"name": v.get("name") or "вариант", "params": override,
                     "score": opt_mod.combo_score(trades, st),
                     "trades": st["stats"]["trades"],
                     "wr": round(100 * st["stats"]["wins"] / max(st["stats"]["trades"], 1), 1),
                     "pnl": round(st["stats"]["pnl"], 2)})
    rows.sort(key=lambda r: -(r.get("score") or -1e9))
    return rows


def _apply_params(applied):
    path = os.path.join(RESULTS, "ai_dev_cfg.json")
    merged = {}
    try:
        with open(path) as f:
            merged = json.load(f)
    except Exception:
        merged = {}
    merged.update({a["param"]: a["value"] for a in applied})
    os.makedirs(RESULTS, exist_ok=True)
    with open(path, "w") as f:
        json.dump(merged, f, indent=1)


def _clamp_actions(actions):
    applied, rejected = [], []
    for a in actions or []:
        if not isinstance(a, dict) or a.get("param") not in DEV_BOUNDS or a.get("value") is None:
            continue
        lo, hi, cast = DEV_BOUNDS[a["param"]]
        try:
            v = max(lo, min(hi, cast(float(a["value"]))))
        except (TypeError, ValueError):
            continue
        applied.append({"param": a["param"], "value": v, "reason": str(a.get("reason", ""))[:100]})
    return applied


# -------------------------------------------------------------- dispatch

def maybe_run(st, trades, cfg, extra):
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "ok": False}
    prev = {}
    if os.path.exists(REPORT_PATH):
        try:
            with open(REPORT_PATH) as f:
                prev = json.load(f)
        except Exception:
            prev = {}
    wait = MIN_INTERVAL_S if prev.get("ok") else RETRY_INTERVAL_S
    try:
        last_ms = time.mktime(time.strptime(prev.get("ts", ""), "%Y-%m-%d %H:%M:%S UTC"))
    except Exception:
        last_ms = 0
    if time.time() - last_ms < wait:
        return {"ok": False, "error": "throttled"}

    t0 = time.time()
    user = ("ДОСЬЕ #1 — АРХИТЕКТУРА КОДА:\n" + _file_dossier()
            + "\n\nДОСЬЕ #2 — СОСТОЯНИЕ И РЕЗУЛЬТАТЫ:\n"
            + json.dumps(_state_digest(st, trades, cfg, extra), ensure_ascii=False))
    answer, err = _ask_llm(user)
    report["latency_s"] = round(time.time() - t0, 1)
    if err:
        report["error"] = err
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        return report
    parsed = _parse_json(answer["text"])
    if not parsed:
        report["error"] = "json не разобран"
        report["raw"] = (answer["text"] or "")[:400]
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        return report

    applied_params = _clamp_actions(parsed.get("param_actions"))

    exp_rows = []
    experiments = parsed.get("experiments")
    experiment_applied = []
    if isinstance(experiments, list) and experiments:
        try:
            variants = [v for v in experiments if isinstance(v, dict) and v.get("params")]
            if variants:
                exp_rows = run_experiments(variants, dict(cfg), days=7)
                base = next((r for r in exp_rows if r["name"].startswith("baseline")), None)
                best = exp_rows[0] if exp_rows else None
                if (best and base and not best["name"].startswith("baseline")
                        and best.get("score", -1e9) > (base.get("score") or -1e9) + 5.0
                        and best.get("trades", 0) >= 15):
                    for k, v in best["params"].items():
                        applied_params.append({"param": k, "value": v,
                                               "reason": f"эксперимент '{best['name']}': score "
                                                         f"{base.get('score')} -> {best.get('score')}"})
                    experiment_applied = [best]
        except Exception as e:
            exp_rows = [{"name": "ошибка песочницы", "error": str(e)[:160], "score": -1e9,
                         "trades": 0, "wr": 0, "pnl": 0}]

    module_info = parsed.get("module")
    module_result = None
    if isinstance(module_info, dict) and module_info.get("code"):
        ns, err2 = validate_module(module_info.get("code"))
        reg = _load_registry()
        n_active = sum(1 for e in reg if e.get("status") == "active")
        if err2:
            module_result = {"ok": False, "error": err2, "purpose": module_info.get("purpose", "")[:150]}
        elif n_active >= MAX_MODULES:
            module_result = {"ok": False, "error": f"лимит {MAX_MODULES} активных модулей (старые нужно уволить)",
                             "purpose": module_info.get("purpose", "")[:150]}
        else:
            name = ns["name"]
            os.makedirs(DEV_DIR, exist_ok=True)
            fpath = os.path.join("ai_modules", name + ".py")
            with open(os.path.join(HERE, fpath), "w") as f:
                f.write(ns["code"])
            reg = [e for e in reg if e.get("name") != name]
            reg.append({
                "name": name, "file": fpath, "status": "active",
                "purpose": str(module_info.get("purpose", ""))[:150],
                "created_ts": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
                "decisions": 0, "vetoes": 0, "fails": 0,
            })
            _save_registry(reg)
            _module_cache["key"] = None
            module_result = {"ok": True, "name": name, "file": fpath,
                             "purpose": str(module_info.get("purpose", ""))[:150]}

    if applied_params:
        _apply_params(applied_params)

    report.update({
        "ok": True,
        "model": answer["model"],
        "thoughts": str(parsed.get("thoughts", ""))[:1000],
        "params_applied": applied_params,
        "experiments": exp_rows,
        "experiment_applied": experiment_applied,
        "module": module_result,
        "modules_active": sum(1 for e in _load_registry() if e.get("status") == "active"),
    })
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(JOURNAL_PATH, "a") as f:
        f.write(json.dumps({
            "ts": report["ts"], "thoughts": report["thoughts"][:300],
            "params": [p["param"] + "=" + str(p["value"]) for p in applied_params],
            "experiments": [{"name": r.get("name"), "score": r.get("score")} for r in exp_rows],
            "module": (module_result or {}).get("name"),
            "module_ok": (module_result or {}).get("ok"),
        }, ensure_ascii=False) + "\n")
    return report
