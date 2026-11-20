import json
import os
import statistics
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(HERE, "results")
DOCS = os.path.join(HERE, "docs")
SIM_STATE_PATH = os.path.join(RESULTS, "sim_state.json")
SIM_TRADES_PATH = os.path.join(RESULTS, "sim_trades.jsonl")
SIM_EQUITY_PATH = os.path.join(RESULTS, "sim_equity.csv")
MODEL_PATH = os.path.join(RESULTS, "model.json")
OPT_PATH = os.path.join(RESULTS, "opt.json")
UNIVERSE_PATH = os.path.join(RESULTS, "universe.json")


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_sim():
    st = None
    if os.path.exists(SIM_STATE_PATH):
        with open(SIM_STATE_PATH) as f:
            st = json.load(f)
    return st, load_jsonl(SIM_TRADES_PATH)


def load_model():
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH) as f:
        return json.load(f)


def load_opt():
    if not os.path.exists(OPT_PATH):
        return None
    with open(OPT_PATH) as f:
        return json.load(f)


def load_universe():
    if not os.path.exists(UNIVERSE_PATH):
        return None
    with open(UNIVERSE_PATH) as f:
        return json.load(f)


def _read(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ago(ms):
    if not ms:
        return "—"
    m = int(max(0, (time.time() * 1000 - ms) / 60000))
    if m < 1:
        return "только что"
    if m < 60:
        return f"{m} мин назад"
    h = m // 60
    return f"{h} ч {m % 60} мин назад" if h < 24 else f"{h // 24} д {h % 24} ч назад"


def pulse_panel():
    b = _read(os.path.join(RESULTS, "beat.json"))
    if not isinstance(b, dict):
        return ""
    sym_rows = ""
    total_bars = 0
    for sym, info in (b.get("symbols") or {}).items():
        if not isinstance(info, dict):
            continue
        la = info.get("last_ago_m", -1)
        la_s = "нет свежих" if (la is None or la < 0) else f"{la} мин назад"
        nb = info.get("bars", 0) or 0
        total_bars += nb
        fresh = "up" if (la is not None and la >= 0 and la <= 6) else ""
        sym_rows += (f"<tr><td class='mono'>{esc(sym)}</td>"
                     f"<td class='muted'>{esc(info.get('src') or '—')}</td>"
                     f"<td class='mono'>{nb}</td>"
                     f"<td class='mono small {fresh}'>{esc(la_s)}</td></tr>")
    opened = b.get("opened") or []
    op_txt = esc(", ".join(opened[-5:])) if opened else "<span class='muted'>нет новых</span>"
    br = b.get("breakers") or {}
    br_ok = br.get("ok", True)
    br_txt = " зелёный" if br_ok else esc(f" стоп: {br.get('reason')}")
    return (f"<div class='chiprow'>"
            f"<span class='chip'>📡 цикл: {esc(b.get('ts', ''))}</span>"
            f"<span class='chip'>свечей в живом буфере: {total_bars}</span>"
            f"<span class='chip'>открыто: {op_txt}</span>"
            f"<span class='chip'>ML-гейт отсеял: {b.get('ml_gated', 0)}</span>"
            f"<span class='chip'>спред-гейт отсеял: {b.get('book_gated', 0)}</span>"
            f"<span class='chip {'chip-ok' if br_ok else 'chip-bad'}'>предохранитель:{br_txt}</span></div>"
            f"<div class='sub' style='margin-top:6px'>это пульс каждого 5-минутного цикла воркера: он проверяет рынок, "
            "ML-гейт пропускает только качественные сигналы, спред-гейт не входит при широком стакане</div>"
            f"<table style='margin-top:8px'><tr><th>Символ</th><th>Источник</th><th>Свечей</th><th>Свежесть бара</th></tr>{sym_rows}</table>")


def data_panel(model):
    cache = os.path.join(RESULTS, "lab", "cache")
    rows = ""
    total_bytes = 0
    total_bars = 0
    if os.path.isdir(cache):
        for fn in sorted(os.listdir(cache)):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(cache, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            total_bytes += sz
            info_s = ""
            if sz < 12_000_000:
                try:
                    with open(p) as f:
                        d = json.load(f)
                    w = d.get("windows") or {}
                    nb = sum(len(v) for v in w.values() if isinstance(v, list))
                    syms = d.get("symbols") or list(w.keys())
                    total_bars += nb
                    info_s = (f"{nb} бар · {esc(', '.join(syms))} · "
                              f"{esc(d.get('days', '—'))} д · скачан {esc(_ago(d.get('ts')))}")
                except Exception:
                    info_s = "—"
            rows += (f"<tr><td class='mono'>{esc(fn)}</td><td class='mono'>{info_s or '—'}</td>"
                     f"<td class='mono muted'>{sz / 1_000_000:.1f} МБ</td></tr>")
    note = ""
    if not rows:
        m = _read(os.path.join(RESULTS, "lab_data.json"))
        if isinstance(m, dict) and m.get("files"):
            note = "<div class='sub' style='margin-top:6px'>история уже скачана ранее и перезаливается при первом бэктесте — ниже последняя известная выгрузка</div>"
            for fn, fi in (m.get("files") or {}).items():
                syms = ', '.join(fi.get('symbols') or [])
                total_bars += fi.get("bars", 0) or 0
                total_bytes += (fi.get("mb", 0) or 0) * 1_000_000
                rows += (f"<tr><td class='mono'>{esc(fn)}</td>"
                         f"<td class='mono'>{fi.get('bars', 0)} бар · {esc(syms)} · "
                         f"{esc(fi.get('days', '—'))} д · скачан {_ago(fi.get('dl_ts'))}</td>"
                         f"<td class='mono muted'>{fi.get('mb', 0)} МБ</td></tr>")
        if not rows:
            rows = "<tr><td colspan='3' class='muted'>скачанной истории пока нет — лаба подпишет её при первом бэктесте</td></tr>"
    ml_n = (model or {}).get("n_trades", 0)
    sim_n = "—"
    ss = _read(SIM_STATE_PATH)
    if isinstance(ss, dict):
        sim_n = (ss.get("stats") or {}).get("trades", "—")
    return (f"<table><tr><th>ML-датасет</th><th>Sim-обучение</th><th>История лаборатории</th></tr>"
            f"<tr><td class='mono'>{ml_n} сделок</td><td class='mono'>{sim_n} сделок</td>"
            f"<td class='mono'>{total_bars} бар · {total_bytes / 1_000_000:.1f} МБ</td></tr></table>"
            f"{note}<table style='margin-top:8px'><tr><th>Файл</th><th>Содержимое</th><th>Размер</th></tr>{rows}</table>")


def lab_progress_panel():
    st_lab = _read(os.path.join(RESULTS, "lab", "state.json"))
    if not isinstance(st_lab, dict):
        return ""
    hunt = st_lab.get("hunt_ts")
    deep = st_lab.get("deep_ts")
    q, qi = st_lab.get("queue") or [], st_lab.get("q_idx", 0)
    q_left = max(0, len(q) - qi)
    hunt_s = f"ещё не запускалась" if not hunt else f"{_ago(hunt)} · порог edge 0.0008 · без LLM"
    deep_s = f"ещё не запускался (90 д на 15m каждые 6 ч)" if not deep else f"{_ago(deep)} · 90 д / 15m"
    escd = st_lab.get("last_escalation")
    esc_s = esc(f"последняя: {escd}") if escd else "триггеры чисты — LLM молчит, детерминер работает сам"
    return (f"<table><tr><th>Где ищет</th><th>Статус</th></tr>"
            f"<tr><td>🏹 Охота за паттернами</td><td class='small'>{hunt_s}</td></tr>"
            f"<tr><td>⏳ Deep-scan устойчивости</td><td class='small'>{esc(deep_s)}</td></tr>"
            f"<tr><td>🧬 Очередь вариантов</td><td class='mono'>{q_left} шт (по 1 за цикл)</td></tr>"
            f"<tr><td>⚡ AI-эскалация</td><td class='small'>{esc_s}</td></tr></table>")


def equity_svg(rows, w=760, h=190):
    if len(rows) < 2:
        return '<div class="empty">Недостаточно данных для графика</div>'
    vals = [float(r["eq"]) for r in rows]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi = lo + 1
    pad = 8
    pts = []
    for i, v in enumerate(vals):
        x = pad + i * (w - 2 * pad) / (len(vals) - 1)
        y = h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    color = "#34d399" if vals[-1] >= vals[0] else "#fb7185"
    area = f"M{pts[0]} L" + " L".join(pts) + f" L{pts[-1].split(',')[0]},{h - pad} L{pad},{h - pad} Z"
    return (f'<svg viewBox="0 0 {w} {h}" class="chart">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{color}" stop-opacity=".25"/>'
            f'<stop offset="1" stop-color="{color}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{area}" fill="url(#g)"/>'
            f'<path d="M{" L".join(pts)}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


def trade_row(t):
    cls = {"win": "up", "loss": "down"}.get(t.get("status"), "")
    pnl = t.get("pnl")
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "—"
    strat = t.get("strategy") or ""
    when = _ago(t.get('closed_ts'))
    return (f"<tr><td class='mono'>{esc(t.get('symbol'))}</td>"
            f"<td class='muted small'>{esc(when)}</td>"
            f"<td><span class='pill {'p-up' if t.get('side')=='long' else 'p-down'}'>{esc(t.get('side','')).upper()}</span></td>"
            f"<td class='mono'>{t.get('entry')}</td><td class='mono'>{t.get('ttl')}s</td>"
            f"<td><span class='pill {'p-up' if t.get('status')=='win' else 'p-down' if t.get('status')=='loss' else ''}'>{esc(t.get('status'))}</span></td>"
            f"<td class='mono {cls}'>{pnl_s}</td>"
            f"<td class='muted'>{esc(strat)}</td></tr>")


def sim_trade_row(t):
    cls = {"win": "up", "loss": "down"}.get(t.get("status"), "")
    pnl = t.get("pnl")
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "—"
    return (f"<tr><td class='mono'>{esc(t.get('symbol'))}</td>"
            f"<td><span class='pill {'p-up' if t.get('side')=='long' else 'p-down'}'>{esc(t.get('side','')).upper()}</span></td>"
            f"<td class='mono'>{t.get('entry')}</td><td class='mono'>{t.get('ttl')}s</td>"
            f"<td><span class='pill {'p-up' if t.get('status')=='win' else 'p-down' if t.get('status')=='loss' else ''}'>{esc(t.get('status'))}</span></td>"
            f"<td class='mono {cls}'>{pnl_s}</td></tr>")


def leaderboard_rows(opt):
    if not opt:
        return ""
    rows = ""
    for r in opt.get("leaderboard", [])[:8]:
        p = r.get("params", {})
        pnl = r.get("pnl", 0)
        cls = "up" if pnl >= 0 else "down"
        rows += (f"<tr><td class='mono'>ATR {p.get('atr_k')} · vol {p.get('vol_spike_min')}</td>"
                 f"<td class='mono'>{r.get('trades')}</td><td class='mono'>{r.get('win_rate')}%</td>"
                 f"<td class='mono'>PF {r.get('profit_factor')}</td>"
                 f"<td class='mono {cls}'>{pnl:+.2f}</td>"
                 f"<td class='mono {'up' if r.get('chunk_worst_pnl', 0) > 0 else 'muted'}'>{r.get('chunk_worst_pnl')}</td></tr>")
    return rows


def generate(st, trades, cfg):
    os.makedirs(DOCS, exist_ok=True)
    import time as _t

    eq_rows = []
    epath = os.path.join(RESULTS, "equity.csv")
    if os.path.exists(epath):
        with open(epath) as f:
            eq_rows = [{"ts": p[0], "eq": p[1]}
                       for p in (l.split(",") for l in f.read().splitlines() if l.strip())]

    s = st["stats"]
    wr = (s["wins"] / s["trades"] * 100) if s["trades"] else 0.0
    gross_w = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    gross_l = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    pnls = [t["pnl"] for t in trades if "pnl" in t]
    sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    sharpe = (statistics.fmean(pnls) / sd * (len(pnls) ** 0.5)) if sd > 0 and pnls else 0.0

    pos_rows = "".join(
        f"<tr><td class='mono'>{esc(p['symbol'])}</td>"
        f"<td class='muted small'>{esc(_ago(p.get('opened_ts')))}</td>"
        f"<td><span class='pill {'p-up' if p['side']=='long' else 'p-down'}'>{p['side'].upper()}</span></td>"
        f"<td class='mono'>{p['entry']}</td><td class='mono'>{p['tp']}</td><td class='mono'>{p['sl']}</td>"
        f"<td class='mono'>{p['ttl']}s</td><td class='mono'>{p['qty']}</td>"
        f"<td class='muted'>{esc(p.get('strategy') or '')} · {esc(p.get('regime') or '')}</td></tr>"
        for p in reversed(st["positions"][-20:]))

    tr_rows = "".join(trade_row(t) for t in reversed(trades[-30:]))
    breaker = "OK" if (st["equity"] and not st.get("paused_until", 0)) else "check state"
    trading = "🟢 Торговля" if not st["positions"] else f"🟢 Торговля · {len(st['positions'])} поз."

    sim_st, sim_trades = load_sim()
    model = load_model()
    opt = load_opt()
    sim_cards = ""
    sim_panel = ""
    if sim_st:
        ss = sim_st["stats"]
        swr = (ss["wins"] / ss["trades"] * 100) if ss["trades"] else 0.0
        sim_eq_rows = []
        if os.path.exists(SIM_EQUITY_PATH):
            with open(SIM_EQUITY_PATH) as f:
                sim_eq_rows = [{"ts": p[0], "eq": p[1]}
                               for p in (l.split(",") for l in f.read().splitlines() if l.strip())]
        sim_rows = "".join(sim_trade_row(t) for t in reversed(sim_trades[-15:]))
        sim_cards = f"""
<div class="card b"><div class="k">Песочница · сделок</div><div class="v">{ss['trades']}</div><div class="d">обучение на истории</div></div>
<div class="card b"><div class="k">Песочница · WR</div><div class="v">{swr:.1f}%</div><div class="d">win rate</div></div>
<div class="card b"><div class="k">Песочница · PnL</div><div class="v {'up' if ss['pnl'] >= 0 else 'down'}">{ss['pnl']:+.2f}</div><div class="d">{esc('equity $' + format(ss.get('equity', 0.0), '.2f'))}</div></div>"""
        sim_panel = f"""
<div class="panel"><div class="ph"><span class="ico">🧪</span>Песочница · sim-эквити</div>{equity_svg(sim_eq_rows)}</div>
<div class="panel"><div class="ph"><span class="ico">🧪</span>Песочница · последние sim-сделки</div>
<table><tr><th>Символ</th><th>Сторона</th><th>Вход</th><th>TTL</th><th>Итог</th><th>PnL $</th></tr>
{sim_rows or '<tr><td colspan="6" class="muted">Sim-сделок пока нет</td></tr>'}</table></div>"""

    upd_ago = max(0, int((_t.time() * 1000 - st.get("updated", 0)) / 60000))
    upd_s = f"обновлено {upd_ago} мин назад" if st.get("updated") else ""
    stale_txt = ("<div class='live' style='color:#fbbf24;border-color:rgba(251,191,36,.4)'>"
                 "⚠ данные устарели</div>" if upd_ago >= 15 else "")

    train_panel = ""
    if model:
        wfo = model.get("wfo", {})
        mc = model.get("monte_carlo") or {}
        age_h = max(0, int((_t.time() * 1000 - model.get("trained_ts", 0)) / 3_600_000))
        train_panel = f"""
<div class="panel"><div class="ph"><span class="ico">🎓</span>ML-гейт · статус обучения</div>
<table><tr><th>Сделок в датасете</th><th>Порог</th><th>Точность (val)</th><th>AUC</th><th>MC p5 PnL</th><th>Обучено</th></tr>
<tr><td class="mono">{model.get('n_trades')}</td><td class="mono">{model.get('threshold')}</td>
<td class="mono">{wfo.get('val_accuracy', '—')}</td><td class="mono">{wfo.get('val_auc', '—')}</td>
<td class="mono">{mc.get('p5_final_pnl', '—')}</td><td class="mono">{age_h} ч назад</td></tr></table>
<div class="sub" style="margin-top:10px">переобучение: ежедневно 02:25 UTC (песочница 30 дней → walk-forward → модель)</div></div>"""

    adapt = st.get("adapt", {})
    adapt_panel = ""
    if adapt:
        arows = ""
        for k in sorted(adapt, key=lambda x: -adapt[x].get("ewma", 0)):
            b = adapt[k]
            on = b.get("n", 0) < 10 or b.get("ewma", 0) > -0.35
            arows += (f"<tr><td class='mono'>{esc(k)}</td><td class='mono'>{b.get('n', 0)}</td>"
                      f"<td class='mono {'up' if b.get('ewma', 0) > 0 else 'down'}'>{b.get('ewma', 0):+.3f}</td>"
                      f"<td><span class='pill {'p-up' if on else 'p-down'}'>{'ON' if on else 'OFF'}</span></td></tr>")
        adapt_panel = f"""
<div class="panel"><div class="ph"><span class="ico">🧠</span>Адаптивный мозг · связки стратегия|режим</div>
<table><tr><th>Связка</th><th>Сделок</th><th>Ewma R</th><th>Торговля</th></tr>
{arows}</table>
<div class="sub" style="margin-top:10px">убыточные связки отключаются сами, пробы 1/40 проверяют их восстановление</div></div>"""

    uni = load_universe()
    uni_panel = ""
    if uni:
        syms = ", ".join(uni.get("symbols", []))
        bk = _read(os.path.join(RESULTS, "book.json")) or {}
        book_html = ""
        for bsym, b in (bk.get("books") or {}).items():
            okb = b.get("spread_bps", 99) <= 8
            book_html += (f"<div class='mono' style='font-size:12px'>"
                          f"{esc(bsym)} · спред {b.get('spread_bps')}bps · "
                          f"{'✓ ок' if okb else '⚠ широ́кий'}</div>")
        uni_panel = f"""
<div class="panel"><div class="ph"><span class="ico">🪐</span>Авто-вселенная монет</div>
<div class="sub">система сама скачивает топ-ликвидные инструменты с Binance (включая золото XAUUSDT, когда оно в топе) и их торговые спецификации · &nbsp;📊 стакан в реальном времени (OKX): вход только если спред узак порога</div>
<p style="font-size:14px;margin-top:8px" class="mono">{esc(syms)}</p>{book_html}</div>"""

    strat_panel = ""
    if opt:
        best_p = opt.get("best", {}).get("params", {})
        rows = leaderboard_rows(opt)
        n_combos = len(opt.get("combo_stats", {}))
        ev = _read(os.path.join(RESULTS, "evo.json")) or {}
        evo_html = ""
        if ev.get("best"):
            eb = ev["best"]
            evo_html = (f"<div class='sub' style='margin-top:8px'>🧬 Эволюция "
                        f"({ev.get('generations')} поколений, {ev.get('evaluations')} оценок): "
                        f"score {eb.get('score')} · pnl {eb.get('pnl')}$ · "
                        f"{'✓ внедрено' if ev.get('robust') else '— неубедительно, не внедряю'} · "
                        f"{esc(', '.join(f'{k}={v}' for k, v in (eb.get('params') or {}).items()))}</div>")
        strat_panel = f"""
<div class="panel"><div class="ph"><span class="ico">⚙️</span>Оптимизатор WFO · лидерборд</div>
<div class="sub">лучшие: ATR {best_p.get('atr_k')} · vol {best_p.get('vol_spike_min')} · адаптивных связок: {n_combos}</div>{evo_html}
<table><tr><th>Параметры</th><th>Сделок</th><th>WR</th><th>PF</th><th>PnL $</th><th>Худший чанк</th></tr>
{rows}</table></div>"""

    ml_card = ""
    if model:
        wfo = model.get("wfo", {})
        ml_card = f"""
<div class="card"><div class="k">ML-гейт</div><div class="v up">ON</div><div class="d">AUC {wfo.get('val_auc', '—')}</div></div>
<div class="card"><div class="k">ML порог</div><div class="v">{model['threshold']}</div><div class="d">точн. {wfo.get('val_accuracy', '—')}</div></div>"""
    else:
        ml_card = """
<div class="card"><div class="k">ML-гейт</div><div class="v muted">OFF</div><div class="d">ждёт данных</div></div>"""

    ai_panel = ""
    rep = _read(os.path.join(RESULTS, "ai_report.json"))
    if rep and rep.get("ok"):
        acts = ", ".join(f"{a['param']} = {a['value']}" for a in rep.get("actions_applied", [])) or "нет правок"
        chip = {"ok": "chip-ok", "warn": "chip-warn", "bad": "chip-bad"}.get(rep.get("verdict"), "chip-warn")
        vtxt = {"ok": "✓ Норма", "warn": "! Внимание", "bad": "✖ Опасность"}.get(rep.get("verdict"), rep.get("verdict", "?"))
        risks = "".join(
            f"<div class='risk'>⚠ {esc(str(r))}</div>"
            for r in rep.get("risks", [])[:4])
        ai_panel = f"""
<div class="panel ai"><div class="ph"><span class="ico">🤖</span>AI-оператор <span class="muted small">· {esc(rep.get("ts", ""))}</span></div>
<div class="chiprow"><span class="chip {chip}">{esc(vtxt)}</span><span class="chip">{esc(rep.get("model", ""))}</span></div>
<p class="lead">{esc(rep.get("summary", ""))}</p>
{risks}
<div class="small muted" style="margin-top:10px">правки параметров: {esc(acts)} · разборов: {sum(1 for l in open(os.path.join(RESULTS, "ai_journal.jsonl"))) if os.path.exists(os.path.join(RESULTS, 'ai_journal.jsonl')) else 0}</div>
</div>"""
    else:
        ai_panel = f"""
<div class="panel ai"><div class="ph"><span class="ico">🤖</span>AI-оператор</div>
<div class="small muted">отчёта нет ({esc(str((rep or {}).get("error", "?")))}) — обновится в следующем цикле</div></div>"""

    dev = _read(os.path.join(RESULTS, "ai_dev_report.json"))
    reg = _read(os.path.join(RESULTS, "ai_modules.json")) or []
    dev_panel = ""
    if isinstance(dev, dict) and dev.get("ok"):
        exp_rows = ""
        for r in (dev.get("experiments") or [])[:4]:
            score = r.get("score")
            score_s = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
            failed = isinstance(score, (int, float)) and score <= -1e8
            params = ", ".join(f"{k}={v}" for k, v in (r.get("params") or {}).items()) or "—"
            exp_rows += (f"<tr><td class='mono'>{esc(r.get('name',''))}</td><td class='mono small'>{esc(params)}</td>"
                         f"<td class='mono'>{r.get('trades')}</td><td class='mono'>{r.get('wr')}%</td>"
                         f"<td class='mono {'up' if (r.get('pnl') or 0) >= 0 else 'down'}'>{r.get('pnl'):+.2f}</td>"
                         f"<td class='mono {'up' if not failed else 'down'}'>{score_s}</td></tr>")
        exp_block = ""
        if exp_rows:
            exp_block = f"""
<div class="sub" style="margin:10px 0 4px">эксперименты в песочнице (бэктест 7 дней, шаг 15 мин):</div>
<table><tr><th>Вариант</th><th>Параметры</th><th>Сделок</th><th>WR</th><th>PnL</th><th>Score</th></tr>
{exp_rows}</table>"""
        mod_rows = ""
        for m in reg:
            status = m.get("status", "?")
            stx = {"active": "p-up", "retired": "muted", "failed": "p-down"}.get(status, "")
            mod_rows += (f"<tr><td class='mono'>{esc(m.get('name',''))}</td>"
                         f"<td class='small'>{esc(m.get('purpose','') or m.get('fail_reason',''))}</td>"
                         f"<td><span class='pill {stx}'>{esc(status)}</span></td>"
                         f"<td class='mono'>{m.get('vetoes', 0)}/{m.get('decisions', 0)} вето</td></tr>")
        mods_block = ""
        if mod_rows:
            mods_block = f"""
<div class="sub" style="margin:10px 0 4px">свои модули-фильтры (авто-отключение при сбое или глушении сигналов):</div>
<table><tr><th>Модуль</th><th>Назначение</th><th>Статус</th><th>Действие</th></tr>
{mod_rows}</table>"""
        params_ai = ""
        if dev.get("params_applied"):
            pa = ", ".join(f"{a['param']}={a['value']}" for a in dev["params_applied"])
            params_ai = f"<div class='good-p'>🛠 внедрено: {esc(pa)}</div>"
        exp_applied = ""
        if dev.get("experiment_applied"):
            b = dev["experiment_applied"][0]
            exp_applied = f"<div class='good-p'>🏆 победитель экспериментов внедрён: {esc(b.get('name',''))} (score {b.get('score')})</div>"
        dev_panel = f"""
<div class="panel ai dev"><div class="ph"><span class="ico">🏗</span>AI-архитектор <span class="muted small">· {esc(dev.get("ts",""))} · {esc(dev.get("model",""))}</span></div>
<p class="lead">{esc(dev.get("thoughts",""))}</p>
{params_ai}{exp_applied}{exp_block}{mods_block}</div>"""

    lab_rep = _read(os.path.join(RESULTS, "lab", "report.json")) or {}
    lab_journal_tail = []
    lp = os.path.join(RESULTS, "lab", "journal.jsonl")
    if os.path.exists(lp):
        try:
            with open(lp) as f:
                for line in f.readlines()[-5:]:
                    d = json.loads(line)
                    det = str(d.get("detail", ""))[:100]
                    pill = "p-down" if d.get("event") == "failed" else ("p-up" if d.get("event") in ("promoted", "checkpoint") else "")
                    lab_journal_tail.append(
                        f"<tr><td class='mono small'>{esc(d.get('ts',''))}</td>"
                        f"<td><span class='pill {pill}'>{esc(d.get('event',''))}</span></td>"
                        f"<td class='small'>{esc(det)}</td></tr>")
        except Exception:
            pass
    lab_panel = ""
    if lab_rep:
        base = lab_rep.get("baseline") or {}
        best = lab_rep.get("best") or {}
        promo = lab_rep.get("promoted") or []
        promo_txt = ""
        if promo:
            last_p = promo[-1]
            if isinstance(last_p, dict):
                if last_p.get("module"):
                    promo_txt = f"<div class='good-p'>🏆 лаборатория внедрила свой модуль: {esc(last_p['module'])}</div>"
                elif last_p.get("params"):
                    pp = ", ".join(f"{k}={v}" for k, v in last_p["params"].items())
                    promo_txt = f"<div class='good-p'>🏆 лаборатория внедрила параметры: {esc(pp)} (score {last_p.get('score')})</div>"
        pats = lab_rep.get("patterns") or {}
        pat_txt = ""
        pat_rows = ""
        for p in (pats.get("top") or []):
            mr = p.get("mean_ret") or 0
            pat_rows += (f"<tr><td class='mono'>{esc(p.get('id',''))}</td>"
                         f"<td class='muted'>{'вверх' if p.get('side')=='long' else 'вниз'}</td>"
                         f"<td class='mono {'up' if mr > 0 else 'down'}'>{mr:+.4%}</td>"
                         f"<td class='mono'>{p.get('n','—')}</td>"
                         f"<td class='mono muted'>{esc(p.get('chunks_ok',''))}</td></tr>")
        if pat_rows:
            pat_txt = (f"<div class='sub' style='margin:10px 0 4px'>золотые закономерности (охота без LLM, "
                       f"{pats.get('count', 0)} шт · {esc(pats.get('ts') or '')}):</div>"
                       f"<table><tr><th>Контекст</th><th>Куда идёт</th><th>Edge/15бар</th><th>n</th><th>Чанки</th></tr>{pat_rows}</table>")
        paper = lab_rep.get("paper")
        paper_txt = ""
        if paper:
            eq = paper.get("equity") or 0
            prc = lab_rep.get("paper_realcheck") or {}
            paper_txt = (f"<table style='margin-top:6px'><tr><th>🤖 Lab-paper (реальные свечи)</th><th>Equity</th>"
                         f"<th>Сделок</th><th>Побед</th><th>PnL</th><th>Реальная сверка</th></tr>"
                         f"<tr><td>{esc(paper.get('active_pack') or 'лучший вариант лабы')}</td>"
                         f"<td class='mono {'up' if eq >= 1000 else 'down'}'>{eq:,.2f}</td>"
                         f"<td class='mono'>{paper.get('trades', 0)}</td>"
                         f"<td class='mono'>{paper.get('wins', 0)}</td>"
                         f"<td class='mono {'up' if (paper.get('pnl') or 0) >= 0 else 'down'}'>{(paper.get('pnl') or 0):+.2f}</td>"
                         f"<td class='mono muted'>{prc.get('trades', '—')} шт · смещение {prc.get('avg_bias', '—')}</td></tr></table>")
        ds = lab_rep.get("deep_scan")
        ds_txt = ""
        if ds:
            v = ds.get("verdict") or "—"
            ds_txt = (f"<div class='{'good-p' if v == 'pass' else 'risk'}'>⏳ Прогон устойчивости: {esc(str(ds.get('days','')))}д/{esc(str(ds.get('tf','')))} — "
                      f"{'устойчив' if v == 'pass' else 'НЕ устойчив'} · baseline {ds.get('score_base','—')}/{ds.get('trades_base','—')} · "
                      f"best {ds.get('score_best','—')}/{ds.get('trades_best','—')} · {esc(str(ds.get('ts','')))}</div>")
        escd = lab_rep.get("escalation")
        esc_txt = ""
        if escd and escd.get("kind"):
            esc_txt = (f"<div class='chip chip-warn'>⚡ AI-эскалация: {escd.get('kind')} "
                       f"(LLM {escd.get('llm_used', 0)}/{lab_rep.get('llm_day_limit', 6)} сегодня)</div>")
        else:
            esc_txt = "<div class='chip chip-ok'>⚡ ИИ на дежурстве — триггеры чисты</div>"
        lab_panel = f"""
<div class="panel dev"><div class="ph"><span class="ico">🧪</span>Лаборатория · Вторая реальность <span class="muted small">· {esc(lab_rep.get('ts',''))}</span></div>
<div class="sub">копия системы живёт своей жизнью: строит гипотезы, пишет модули-фильтры, качает данные с бирж, гоняет бэктесты; всё, что стабильно обгоняет оригинал, переносится сюда (авто-откат к лучшему чекпоинту)</div>
<table style="margin-top:8px"><tr><th>Фаза</th><th>Шагов</th><th>LLM сегодня</th><th>В очереди</th><th>Чекпоинты</th></tr>
<tr><td class='mono'>{esc(lab_rep.get('phase','—'))}</td><td class='mono'>{lab_rep.get('steps',0)}</td>
<td class='mono'>{lab_rep.get('llm_used_today',0)}/{lab_rep.get('llm_day_limit',6)}</td>
<td class='mono'>{lab_rep.get('queue_left',0)}</td><td class='mono'>{lab_rep.get('checkpoints',0)}</td></tr></table>
<table style="margin-top:6px"><tr><th>Вариант</th><th>Score</th><th>Сделок</th><th>PnL $</th></tr>
<tr><td>оригинал (baseline)</td><td class='mono'>{base.get('score','—')}</td><td class='mono'>{base.get('trades','—')}</td>
<td class='mono {'up' if (base.get('pnl') or 0) > 0 else 'down'}'>{base.get('pnl','—')}</td></tr>
<tr><td>{esc(best.get('name') or 'лучшая мутация')}</td><td class='mono'>{best.get('score','—')}</td><td class='mono'>{best.get('trades','—')}</td>
<td class='mono {'up' if (best.get('pnl') or 0) > 0 else 'down'}'>{best.get('pnl','—')}</td></tr></table>
{esc_txt}{promo_txt}{ds_txt}{pat_txt}{paper_txt}
<div class="sub" style="margin:10px 0 4px">журнал шагов:</div>
<table><tr><th>Время</th><th>Событие</th><th>Детали</th></tr>{''.join(lab_journal_tail)}</table></div>"""

    vcheck = _read(os.path.join(RESULTS, "real_check.json"))
    vcheck_panel = ""
    if isinstance(vcheck, dict) and vcheck.get("trades"):
        vrows = ""
        for o in reversed(vcheck["trades"][-15:]):
            amb = "⚠ двусмысленно" if o.get("ambiguous") else ""
            match = o.get("match")
            mtxt = "✓" if match else "✗ расходится"
            mcls = "up" if match else "down"
            pr = o.get("pnl_real") or 0
            vrows += (f"<tr><td class='mono'>{esc(o.get('symbol',''))}</td>"
                      f"<td class='muted'>{esc(o.get('opened',''))}</td>"
                      f"<td><span class='pill {'p-up' if o.get('kind_real')=='win' else 'p-down' if o.get('kind_real')=='loss' else ''}'>{esc(o.get('kind_real') or o.get('status_recorded',''))}</span></td>"
                      f"<td class='mono {'up' if (o.get('pnl_recorded') or 0) >= 0 else 'down'}'>{(o.get('pnl_recorded') or 0):+.3f}</td>"
                      f"<td class='mono {'up' if pr >= 0 else 'down'}'>{pr:+.3f}</td>"
                      f"<td class='mono {mcls}'>{mtxt}</td><td class='muted small'>{amb}</td></tr>")
        sm = vcheck.get("summary") or {}
        n_checked = sm.get("trades", 0)
        vrows_tot = (f"<tr><td><b>TOTAL</b></td><td></td><td class='mono'><b>{n_checked}</b> проверено</td>"
                     f"<td class='mono {'up' if sm.get('recorded_total', 0) >= 0 else 'down'}'><b>{sm.get('recorded_total', 0):+.2f}</b></td>"
                     f"<td class='mono {'up' if sm.get('real_total', 0) >= 0 else 'down'}'><b>{sm.get('real_total', 0):+.2f}</b></td>"
                     f"<td class='mono muted'>{sm.get('realism_bias', 0):+.2f} (смещение)</td><td></td></tr>")
        vcheck_panel = f"""
<div class="panel"><div class="ph"><span class="ico">🔍</span>Авто-проверка на реальных биржевых данных</div>
<div class="sub">каждая сделка автоматически пересчитывается по реальным 1m-свечам Binance/Bybit: вход по следующему бару с проскальзыванием, полная тейкер-комиссия обе стороны, стоп глубже на проскальзывание. этими данными питаются ИИ-мозги · обновлено: {esc(sm.get('updated',''))}</div>
<table style="margin-top:8px"><tr><th>Символ</th><th>Открыта</th><th>Реальный итог</th><th>Записано</th><th>Реально</th><th>Сходится</th><th></th></tr>
{vrows}{vrows_tot}</table></div>"""

    pulse = pulse_panel()
    if pulse:
        pulse = ('<div class="panel"><div class="ph"><span class="ico">📡</span>Пульс системы '
                 '<span class="muted small">· цикл воркера</span></div>' + pulse + '</div>')
    data_p = data_panel(model)
    if data_p:
        data_p = ('<div class="panel"><div class="ph"><span class="ico">🗄️</span>Данные '
                  '<span class="muted small">· что скачано и на чём обучается</span></div>' + data_p + '</div>')
    lprog = lab_progress_panel()
    if lprog:
        lprog = ('<div class="panel"><div class="ph"><span class="ico">🔎</span>Лаборатория · где ищет прямо сейчас</div>'
                 + lprog + '</div>')
    html = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#070a12">
<title>AEGIS-Lab PRO · Adaptive Fee-Edge</title>
<style>
:root{{--bg:#070a12;--card:rgba(255,255,255,.035);--card2:rgba(255,255,255,.055);
--line:rgba(255,255,255,.08);--line2:rgba(255,255,255,.14);--txt:#eef2fa;--muted:#8ba0c0;
--up:#34d399;--down:#fb7185;--acc:#6d7cff;--acc2:#22d3ee;
--grad:linear-gradient(135deg,#6d7cff 0%,#22d3ee 100%)}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{-webkit-text-size-adjust:100%}}
body{{background:var(--bg);color:var(--txt);
font:15px/1.55 "Inter","SF Pro Display",-apple-system,"Segoe UI",Roboto,sans-serif;
background-image:radial-gradient(900px 500px at 85% -10%,rgba(109,124,255,.16),transparent 60%),
radial-gradient(700px 400px at -10% 0%,rgba(34,211,238,.10),transparent 55%),
radial-gradient(900px 600px at 50% 120%,rgba(52,211,153,.07),transparent 60%);
background-attachment:fixed;padding:0 16px 32px}}
.wrap{{max-width:920px;margin:0 auto}}
header.bar{{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px;
padding:14px 4px 12px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
background:rgba(7,10,18,.72);border-bottom:1px solid var(--line)}}
.brand{{font-weight:800;font-size:19px;letter-spacing:.4px;
background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent}}
.brand small{{display:block;font-size:10px;font-weight:600;letter-spacing:2.2px;color:var(--muted);
background:none;-webkit-background-clip:unset;-webkit-text-fill-color:var(--muted)}}
.live{{display:flex;align-items:center;gap:7px;background:var(--card2);border:1px solid var(--line2);
border-radius:99px;padding:5px 13px;font-size:12px;font-weight:600}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--up);box-shadow:0 0 0 0 rgba(52,211,153,.6);
animation:pulse 2s infinite}}
@keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(52,211,153,.55)}}70%{{box-shadow:0 0 0 9px rgba(52,211,153,0)}}100%{{box-shadow:0 0 0 0 rgba(52,211,153,0)}}}}
.timer{{color:var(--muted);font-variant-numeric:tabular-nums}}
.hero{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:14px;padding:22px 4px 6px}}
.hero .eq{{font-size:15px;color:var(--muted)}}
.hero .eq b{{display:block;font-size:40px;font-weight:800;letter-spacing:-1px;color:var(--txt);
font-variant-numeric:tabular-nums}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px 18px;
transition:transform .18s,border-color .18s}}
.card:hover{{transform:translateY(-2px);border-color:var(--line2)}}
.card .k{{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1.1px;font-weight:600}}
.card .v{{font-size:24px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.3px}}
.card .d{{color:var(--muted);font-size:12px;margin-top:2px}}
.card.hl{{background:linear-gradient(135deg,rgba(109,124,255,.14),rgba(34,211,238,.08));
border-color:rgba(109,124,255,.35)}}
.up{{color:var(--up)}}.down{{color:var(--down)}}.muted{{color:var(--muted)}}
.mono{{font-variant-numeric:tabular-nums}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin:14px 0}}
.panel.ai{{border-color:rgba(109,124,255,.28);background:linear-gradient(160deg,rgba(109,124,255,.08),var(--card) 55%)}}
.panel.dev{{border-color:rgba(34,211,238,.25);background:linear-gradient(160deg,rgba(34,211,238,.07),var(--card) 55%)}}
.ph{{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;display:flex;align-items:center;gap:9px}}
.ph .ico{{font-size:17px}}
.ph .small{{text-transform:none;letter-spacing:0;font-weight:500}}
.lead{{font-size:14.5px;line-height:1.6}}
.chiprow{{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 10px}}
.chip{{border:1px solid var(--line2);border-radius:99px;padding:3px 12px;font-size:12px;font-weight:600;background:var(--card2)}}
.chip-ok{{color:var(--up);border-color:rgba(52,211,153,.4)}}
.chip-warn{{color:#fbbf24;border-color:rgba(251,191,36,.4)}}
.chip-bad{{color:var(--down);border-color:rgba(251,113,133,.4)}}
.good-p{{background:rgba(52,211,153,.09);border:1px solid rgba(52,211,153,.3);color:var(--up);
border-radius:10px;padding:7px 11px;font-size:12.5px;font-weight:600;margin:8px 0}}
.risk{{color:var(--down);font-size:12.5px;margin-top:4px}}
.chart{{width:100%;height:auto;margin-top:4px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{color:var(--muted);text-align:left;font-weight:600;font-size:11.5px;text-transform:uppercase;
letter-spacing:.6px;padding:7px 8px;border-bottom:1px solid var(--line2)}}
td{{padding:8px;border-bottom:1px solid rgba(255,255,255,.05)}}
tr:last-child td{{border-bottom:none}}
.pill{{display:inline-block;border-radius:99px;padding:1px 9px;font-size:11px;font-weight:700;
background:var(--card2);border:1px solid var(--line2)}}
.p-up{{color:var(--up);border-color:rgba(52,211,153,.4)}}
.p-down{{color:var(--down);border-color:rgba(251,113,133,.4)}}
.empty{{color:var(--muted);font-size:13px;padding:8px 0}}
.nav{{display:flex;gap:8px;overflow-x:auto;white-space:nowrap;padding:12px 2px 0;scrollbar-width:none}}
.nav::-webkit-scrollbar{{display:none}}
.nav a{{flex:0 0 auto;font-size:12.5px;font-weight:600;color:var(--muted);background:var(--card);\nborder:1px solid var(--line);border-radius:99px;padding:6px 13px;text-decoration:none}}
.nav a:hover{{color:var(--txt);border-color:var(--line2);text-decoration:none}}
html{{scroll-behavior:smooth}}
[id]{{scroll-margin-top:74px}}
summary{{cursor:pointer;font-weight:600;font-size:13px}}
.small{{font-size:12.5px}}.sub{{color:var(--muted);font-size:12.5px}}
footer{{color:var(--muted);font-size:12px;margin-top:26px;text-align:center;line-height:1.8}}
a{{color:var(--acc2);text-decoration:none}}a:hover{{text-decoration:underline}}
@media (max-width:640px){{
body{{padding:0 10px 24px;font-size:14px}}
.hero .eq b{{font-size:32px}}
.grid{{grid-template-columns:repeat(2,1fr);gap:9px}}
.card{{padding:12px 13px;border-radius:15px}}.card .v{{font-size:19px}}
.panel{{padding:13px;border-radius:15px}}
.ph{{font-size:12.5px}}
table{{font-size:11.5px;display:block;overflow-x:auto;white-space:nowrap;-webkit-overflow-scrolling:touch}}
th,td{{padding:5px 6px}}
}}
</style></head><body><div class="wrap">
<header class="bar">
<div class="brand">AEGIS<small>PAPER TRADING LAB</small></div>
<div class="live"><span class="dot"></span><span class="timer" id="tm">120s</span></div>
<div class="live muted">{trading}</div>
{stale_txt}
</header>
<nav class="nav">
<a href="#top">Обзор</a><a href="#pulse">📡 Пульс</a><a href="#data">🗄 Данные</a><a href="#lab">🧪 Лаборатория</a><a href="#lprog">🔎 Где ищет</a><a href="#ai">🤖 AI-оператор</a><a href="#dev">🏗 Архитектор</a><a href="#vcheck">🔍 Реал-проверка</a><a href="#charts">📈 Эквити</a><a href="#positions">🎯 Позиции</a><a href="#trades">💰 Сделки</a><a href="#wfo">⚙ WFO</a><a href="#uni">🪙 Монеты</a><a href="#train">🎓 ML</a><a href="#adapt">🧠 Адаптация</a>
</nav>
<div class="hero" id="top">
<div class="eq">Equity (paper)<b>${st['equity']:.2f}</b></div>
<div class="eq" style="text-align:right">Binance → Bybit → OKX<br>
<span class="muted small">риск {cfg['risk_per_trade']} USDT · RR 1:{cfg.get('rr', 1.0)} · Fee-Edge ×{cfg.get('fee_edge_mult', 3.0)} · TTL {cfg.get('ttl_min', 300)}–{cfg.get('ttl_max', 900)}с · {upd_s}</span></div>
</div>
<div class="grid">
<div class="card hl"><div class="k">Win Rate</div><div class="v {'up' if wr >= 50 else ''}">{wr:.1f}%</div><div class="d">{s['wins']}W / {s['losses']}L</div></div>
<div class="card"><div class="k">Profit Factor</div><div class="v">{'∞' if pf == float('inf') else f'{pf:.2f}'}</div><div class="d">прибыль/убыток</div></div>
<div class="card"><div class="k">Сделок</div><div class="v">{s['trades']}</div><div class="d">paper-циклы 5 мин</div></div>
<div class="card"><div class="k">PnL (paper)</div><div class="v {'up' if s['pnl'] >= 0 else 'down'}">{s['pnl']:+.2f}</div><div class="d">суммарно по сделкам</div></div>
<div class="card"><div class="k">Sharpe</div><div class="v">{sharpe:.2f}</div><div class="d">paper</div></div>
<div class="card"><div class="k">Серия убытков</div><div class="v {'down' if st['consec_losses'] > 0 else ''}">{st['consec_losses']} / {cfg['loss_streak_pause']}</div><div class="d">стоп-лимит</div></div>
{ml_card}{sim_cards}
</div>
<div id="pulse">{pulse}</div><div id="data">{data_p}</div>
<div id="ai">{ai_panel}</div><div id="dev">{dev_panel}</div><div id="lab">{lab_panel}{lprog}</div><div id="vcheck">{vcheck_panel}</div>
<div id="charts"><div class="panel"><div class="ph"><span class="ico">📈</span>Кривая эквити</div>{equity_svg(eq_rows)}</div></div>
<div id="sim">{sim_panel}</div>
<div id="positions"><div class="panel"><div class="ph"><span class="ico">🎯</span>Открытые позиции ({len(st['positions'])}/{cfg['max_concurrent']})</div>
<div class="sub">движок проверяет рынок каждые 5 минут; сигнал приходит в среднем раз в 30–90 минут — когда условия совпадут, сделка появится здесь автоматически</div>
<table style="margin-top:8px"><tr><th>Символ</th><th>Открыта</th><th>Сторона</th><th>Вход</th><th>TP</th><th>SL</th><th>TTL</th><th>Qty</th><th>Стратегия · режим</th></tr>
{pos_rows or '<tr><td colspan="9" class="muted">Позиций нет — ждём сигнал (проверка каждые 5 мин)</td></tr>'}</table></div></div>
<div id="trades"><div class="panel"><div class="ph"><span class="ico">💰</span>Последние сделки</div>
<table><tr><th>Символ</th><th>Когда</th><th>Сторона</th><th>Вход</th><th>TTL</th><th>Итог</th><th>PnL $</th><th>Стратегия</th></tr>
{tr_rows or '<tr><td colspan="8" class="muted">Сделок пока нет — воркер собирает сигналы</td></tr>'}</table></div></div>
<div id="wfo">{strat_panel}</div><div id="uni">{uni_panel}</div><div id="train">{train_panel}</div><div id="adapt">{adapt_panel}</div>
<details class="panel"><summary>❔ как читать панель — коротко о каждом блоке</summary>
<div class="small muted" style="margin-top:10px;line-height:1.9">
<b>Обзор</b> — equity, винрейт, профит-фактор, серии убытков, ML-гейт ·
<b>🤖 AI-оператор</b> — что ИИ решил и поменял в последнем цикле (лимит 6 LLM/день) ·
<b>🏗 AI-архитектор</b> — модули-фильтры и эксперименты, написанные ИИ ·
<b>🧪 Лаборатория</b> — «вторая реальность»: копия системы ищет и проверяет улучшения, стабильно лучшие внедряются в оригинал ·
<b>⚡ Эскалация</b> — детерминированные триггеры (просадка, смещение ML, золото) будят ИИ в нужный момент ·
<b>📡 Пульс / 🗄 Данные</b> — что происходит в цикле, что скачано и на чём обучение ·
<b>🔍 Реал-проверка</b> — пересчёт сделок по реальным свечам с комиссиями и проскальзыванием ·
<b>⚙ WFO / 🧠 Адаптация</b> — оптимизация параметров и автоприостановка убыточных связок ·
<b>🎓 ML-гейт</b> — модель пропускает только качественные сигналы</div></details>
<footer>Предохранитель: {esc(breaker)} · paper-only, не реальные деньги ·
<a href="https://github.com/palcikia-crypto/aegis-lab">репозиторий</a> ·
зеркало: <a href="https://aegis-lab.pages.dev">aegis-lab.pages.dev</a> · AI-мозги: Cloudflare Workers AI</footer>
<script>var __t=120;setInterval(function(){{__t-=1;if(__t<=0){{location.reload();return;}}var e=document.getElementById('tm');if(e)e.textContent=__t+'s';}},1000);</script>
</div></body></html>"""
    for name in ("index.html", "pro.html"):
        with open(os.path.join(DOCS, name), "w") as f:
            f.write(html)
