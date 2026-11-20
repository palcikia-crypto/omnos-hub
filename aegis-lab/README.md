# AEGIS-Lab v3 PRO · Adaptive Fee-Edge Engine (Binance/OKX/Bybit, GitHub Actions)

Paper-версия «AEGIS-1:1 QUANT ENGINE» под бюджет $0: весь движок живёт в GitHub
Actions на публичных рыночных данных, без API-ключей и без реальных денег.
v3 PRO — система сама развивается: скачивает нужные монеты и их торговые
спецификации, ищет эдж по режимам рынка и защищает прибыль от комиссий.

## Архитектура v3 PRO
- **Данные**: Binance USDT-M futures (лучшие лимиты/надёжность) → Bybit → OKX,
  автоматический фолбэк. Ключи не нужны.
- **Авто-вселенная** (`src/universe.py`): система сама скачивает топ-ликвидные
  USDT-перпы (по объёму 24ч, включая золото XAUUSDT, когда оно в топе) и их
  торговые спецификации (шаг лота, мин. количество, мин. нотинал) —
  ничего вручную конфигурировать не нужно.
- **Fee-Edge Envelope** (защита от комиссий): сделка открывается только если
  целевое движение ≥ N× круговых издержек (комиссии + спред-буфер).
  Бэктест: тот же период, тот же движок — без конверта −$32, с конвертом
  (N=5) +$3.2 при WR 52%. Оптимизатор сам подбирает N.
- **Адаптивный RR и TTL**: TP = rr×D (rr из WFO), а TTL вычисляется из
  «физики» — времени, за которое цена проходит D при текущем ATR-темпе
  (с буфером). Убивает главную утечку — таймауты до касания TP/SL.
- **Режим рынка** (`src/regime.py`): ADX(15m) + перцентиль ATR →
  trend_up / trend_down / range / volatile.
- **Стратегии** (`src/strategies.py`): baseline (объём+EMA+моментум),
  mean_reversion (BB+RSI, только рейндж), breakout (Donchian 15m + объём,
  против тренда не торгует), trend_follow (ADX-подтверждённый моментум).
- **Адаптивный мозг**: ewma R-мультипликатора по каждой связке
  «стратегия|режим»; связки с отрицательным ожиданием отключаются сами,
  при деградации снова включаются. Сид — из песочницы (opt.json).
- **Оптимизатор WFO** (`src/optimize.py`): грид (ATR k × vol-порог),
  робастность = прибыльность в каждом временном чанке + PF + WR.
  Лучшие параметры воркер подхватывает автоматически.
- **ML-гейт** (`src/train.py`): логрегрессия на 8 признаках, WFO 70/30,
  Monte Carlo 1000 прогонов. Фильтрует слабые сигналы в живом цикле.

## Что внутри
- **Движок 1:1** (`src/engine.py`): риск фиксированный в USDT, TP/SL = ±D,
  D = k×ATR(15m), экспирация 300/900 сек, принудительное закрытие по таймеру.
- **Признаки** (`src/features.py`): EMA9/21, моментум, всплеск объёма, ATR15.
  Признаки пишутся в каждую сделку — датасет для будущего ML-фильтра (Этап 3).
- **Данные** (`src/market.py`): Bybit V5 public REST (bytick → bybit),
  фолбэк на OKX public candles. Ключи не нужны.
- **Предохранители**: 6 убытков подряд → пауза 60 мин; −5% от дневного
  баланса → стоп до 00:00 UTC; максимум 3 одновременных позиции.
- **Дашборд**: `docs/index.html` (GitHub Pages), перегенерируется каждый цикл.
- **Надёжность**: state.json и trades.jsonl коммитятся каждый прогон,
  краши пишутся в results/crash.log и валят воркфлоу (не глотаются).

## Запуск
Воркер запускается автоматически каждые 5 минут (`.github/workflows/aegis.yml`).
Локально: `python -m src.worker`.

## Дашборд
Включите GitHub Pages: Settings → Pages → Source: `main` / `/docs`.
Панель: https://palcikia-crypto.github.io/aegis-lab/

## Cloudflare Pages
Панель дополнительно деплоится на Cloudflare Pages (`.github/workflows/cloudflare-pages.yml`).

Требуемые секреты репозитория (Settings → Secrets and variables → Actions):
- `CLOUDFLARE_API_TOKEN` — API-токен Cloudflare (Account API Tokens)
- `CLOUDFLARE_ACCOUNT_ID` — Account ID
- опционально для R2: `CLOUDFLARE_R2_ACCESS_KEY_ID`, `CLOUDFLARE_R2_SECRET_ACCESS_KEY`

Повторное использование в любом другом репозитории (секреты должны быть заданы в нём же):

```yaml
jobs:
  deploy:
    uses: palcikia-crypto/aegis-lab/.github/workflows/cloudflare-pages.yml@main
    with:
      directory: docs
      project_name: my-project
    secrets: inherit
```

## Дорожная карта (по ТЗ)
1. ✅ Этап 1: базовый каркас + калькулятор риска + таймеры (этот репо, paper).
2. ✅ Этап 2 (частично): запись признаков каждой сделки → датасет.
3. ✅ Этап 3: ML-фильтр на собранных логах, WFO + Monte Carlo (pure-python
   логистическая регрессия, без зависимостей).
4. ⏳ Этап 4: реальный депозит — только после gold-проверки, ключи через Secrets.

## Песочница (ускоренное обучение)
Живой воркер набирает датасет неделями. Песочница (`src/sim.py`) решает это:
реплей истории 1m-свечей через тот же движок (те же комиссии, риск,
предохранители), но на исторических данных — 14 дней × 3 символа прогоняются
за ~минуту. Сделки пишутся в `results/sim_trades.jsonl` с префиксом `sim-` и
пометкой `source: "sim"`; живое paper-состояние не затрагивается.

```bash
python -m src.optimize --days 30  # WFO-грид параметров → opt.json
python -m src.sim --days 30       # реплей истории в песочницу
python -m src.train               # обучение ML-гейта (sim + live сделки)
python -m src.worker              # живой цикл
```

`src/train.py` (Этап 3): логистическая регрессия на признаках сделок,
walk-forward 70/30, Monte Carlo 1000 прогонов по PnL. Модель пишется в
`results/model.json`. Когда модель есть, живой воркер гейтит сигналы:
сигнал открывается только если вероятность победы ≥ порога из валидации.
Без модели поведение воркера не меняется. В Actions песочница + обучение
запускаются ежедневно (cron 02:25 UTC) и коммитят датасет с моделью.

**Paper-only.** Это исследовательский стенд, не инвестиционная рекомендация.
## Cloudflare Workers
`cloudflare/gemini-proxy/worker.js` — прокси Google Gemini API через Cloudflare (обход региональных блокировок).
Живой воркер: https://gemini-proxy.apigemenipp.workers.dev

Использование: заменить базовый URL `https://generativelanguage.googleapis.com` на адрес воркера,
API-ключ Gemini передаётся как обычно (`x-goog-api-key` или `?key=`). CORS включён — работает из браузера.

Пример:
```
GET https://gemini-proxy.apigemenipp.workers.dev/v1beta/models?key=ВАШ_GEMINI_KEY
POST https://gemini-proxy.apigemenipp.workers.dev/v1beta/models/gemini-2.0-flash:generateContent?key=ВАШ_GEMINI_KEY
```

Деплой из репозитория (локально): `npx wrangler@3 deploy cloudflare/gemini-proxy/worker.js --name gemini-proxy --compatibility-date 2024-12-01`

## Авто-проверка на реальных данных (fees & slippage)
Каждая закрывшаяся сделка автоматически пересчитывается по реальным 1m-свечам биржи
(`src/verify.py`, запускается в каждом цикле воркера): вход по следующему бару с
неблагоприятным проскальзыванием (1 bp), полная тейкер-комиссия обе стороны (0.06%),
стоп/таймаут глубже на проскальзывание (2 bp), TP без проскальзывания (лимит);
касание TP и SL в одном баре = консервативный убыток. Сводка (results/real_check.json)
идёт в ИИ-мозги (ai_advisor, ai_dev) для самоулучшения и в панель
«🔍 Авто-проверка на реальных биржевых данных» дашборда. Движок paper-PnL считает
честно: полная комиссия + adverse slippage (src/engine.py). Первая сводка (2026-11-18):
5 сделок, записано +2.18, реально +3.30, расхождений итога и двусмысленностей нет.

## Эволюция стратегий и стакан (order book)
**Эволюция** (`src/evo.py`, ежедневная 12-минутная рядом с train) мутирует и скрещивает
параметры стратегий (atr_k, rr, vol_spike_min, fee_edge_mult, ttl_min/max); выживает только
мутант с устойчивым score по WFO-чанкам — «повезло на окне» гибнет. Победитель идёт в
results/evo.json и подмешивается в конфиг воркера (ниже правок ИИ).

**Стакан в реальном времени** (`src/book.py`, OKX books): каждый цикл кандидат на вход
проверяется по реальному спреду (порог max_spread_bps) — при широком стакане вход запрещён;
снимок стакана (спред, imbalance, глубина $) лежит в results/book.json и показан в панели.

## AI-мозг: оператор + архитектор (Workers AI)
Раз в 15 минут (или сразу после закрытия новой сделки) `src/ai_advisor.py` отправляет компактный слепок состояния
в **Workers AI** (`@cf/meta/llama-3.3-70b-instruct-fp8-fast`, бесплатно 10 000 Neurons/день)
и получает разбор на русском: вердикт (ok / warn / bad), summary, риски. Модель может
предложить правки параметров — применяются только из белого списка с жёсткими границами.
Приоритет конфига: settings.json → opt.json → **results/ai_op_cfg.json (оператор)** → **results/ai_dev_cfg.json (архитектор, высший)**.

Циклы запускаются каждые ~5 минут через самотриггер: коммит бота сам запускает
следующий цикл (cron остался как резерв — GitHub-расписание заметно тормозит).
Сигналы считаются **только по закрытой 1m-свече** (недокрытая не участвует).

Раз в час запускается **AI-архитектор** (`src/ai_dev.py`, «бог-режим»):
- строит досье всей системы (структура кода, сигнатуры, конфиг, результаты) — ИИ видит систему целиком;
- ставит бэктест-**эксперименты**: 7 дней истории через движок в песочнице, варианты параметров от ИИ,
  робастный score; победитель внедряется (только в границах безопасных диапазонов);
- **пишет свои модули-фильтры** (`ai_modules/*.py`): запрет опасных конструкций → compile →
  exec в изоляции → smoke-тест на мок-фичах; модуль умеет только ВЕТО на сделку,
  авто-отключается после 2 сбоев или при глушении >70% сигналов (лимит 3 активных).
Отчёты: results/ai_report.json, results/ai_dev_report.json, results/ai_modules.json,
results/ai_*_journal.jsonl — всё видно в панелях «🤖 AI-оператор» и «🏗 AI-архитектор» дашборда.
Env для воркера: `WAI_TOKEN`, `WAI_ACCOUNT` (секреты `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`) —
без них AI-разбор пропускается, торговля не прерывается.

## VPN Guard (самолечение узлов)
`.github/workflows/vpn-guard.yml` — cron каждые 15 минут: health-check всех воркеров
(vless-vpn, aegis-vpn, vpn-uni, vpn-phone, vless-auto, vless-turbo, gemini-api);
зависший воркер автоматически удаляется и пересоздаётся из кода в репозитории
(лечит известную проблему «залипания» воркеров аккаунта). Результат — в job summary.

## АВТО-узел (vless-auto)
`cloudflare/vless-auto/` + `docs/vpn/auto-config.json` — полный Xray-конфиг для v2rayNG
(импорт конфигурации из буфера): geoip/geosite RU + банки/Госуслуги/маркетплейсы — напрямую,
остальное — через vless-auto (порт 80, `ed=2048`). Ручной выбор приложений не нужен.

## Gemini Чат (замена AI Studio)
`docs/chat.html` — веб-чат с Gemini на Cloudflare Pages, работает из РФ без VPN:
запросы идут через прокси-воркер `gemini-api` на Google API (API-эндпоинт не блокирует
Cloudflare-IP, в отличие от веб-интерфейса AI Studio). API-ключ Gemini хранится только
в localStorage браузера пользователя, в репозиторий и воркер не попадает.

## VLESS VPN (Workers)
`cloudflare/vless-vpn/worker.js` — VLESS over WebSocket + TLS на Cloudflare Workers (TCP + DNS).
Два узла — основной `vless-vpn` и запасной `aegis-vpn` (одинаковый код, оба прошли E2E). Плюс универсальный узел `vpn-uni` для Google AI Studio и прочего, телефонный узел `vpn-phone` для постоянного VPN (Always-on) на Android и турбо-узел `vless-turbo` (0-RTT early-data, ответ без ожидания записи) в `cloudflare/vless-turbo/`. UUID задан в коде (константа `UUID`).

Импорт клиента (v2rayNG / Hiddify / v2rayN): добавить по ссылке или вручную:
```
vless://f30d23f3-92c3-4147-86de-b76f36f718ed@vless-vpn.apigemenipp.workers.dev:443?encryption=none&security=tls&sni=vless-vpn.apigemenipp.workers.dev&fp=chrome&type=ws&host=vless-vpn.apigemenipp.workers.dev&path=%2F#AegisVPN-1
vless://f30d23f3-92c3-4147-86de-b76f36f718ed@aegis-vpn.apigemenipp.workers.dev:443?encryption=none&security=tls&sni=aegis-vpn.apigemenipp.workers.dev&fp=chrome&type=ws&host=aegis-vpn.apigemenipp.workers.dev&path=%2F#AegisVPN-2
vless://f30d23f3-92c3-4147-86de-b76f36f718ed@vpn-uni.apigemenipp.workers.dev:80?encryption=none&security=none&type=ws&host=vpn-uni.apigemenipp.workers.dev&path=%2F#Aegis-UNI
vless://f30d23f3-92c3-4147-86de-b76f36f718ed@vpn-phone.apigemenipp.workers.dev:80?encryption=none&security=none&type=ws&host=vpn-phone.apigemenipp.workers.dev&path=%2F#Aegis-PH
```
Для РФ: `*.workers.dev` часто блокируется провайдерами — в v2rayNG включить фрагментацию (Настройки → Фрагментация / TLS Fragment), маршрутизация «Глобальный», проверка — «Тест реальной задержки» долгим нажатием на конфиг. Надёжное решение — собственный домен в Cloudflare (Workers Custom Domain).
Проверка живости: `GET https://vless-vpn.apigemenipp.workers.dev/` или `GET https://aegis-vpn.apigemenipp.workers.dev/` → JSON со UUID.

## Проверка на реальных данных (fees & slippage)
Каждая закрывшаяся сделка пересчитывается по реальным 1m-свечам Binance
(`results/real_check.json`, панель «🔍 Проверка на реальных биржевых данных»):
вход по следующему бару с проскальзыванием, тейкер-комиссия обе стороны
(fee_rate 0.06%), стопы глубже на проскальзывание (slippage_rate 0.01%).
Движок (src/engine.py) считает paper-PnL честно: полная комиссия + adverse slippage
на вход и на стоп/таймаут, TP без проскальзывания (лимит). Итог первой проверки
5 сделок (2026-11-17): записано +2.18 → реально +3.43 / пессимистично +3.23.
