# ⚡ OmniLab Hub

Единый хаб всех проектов. Каждый каталог — точная копия (1:1) оригинального репозитория
на момент создания хаба. **Оригиналы больше не трогаем: разработка и улучшения — только здесь.**

> Статус: воркеры запущены (aegis 5 мин, pm-lab 2×/час, bt-worker 30 мин, arb-scan 3 ч), синтаксис монолита под охраной CI, Pages активен.

| Каталог | Проект | Оригинал |
|---|---|---|
| `quantum_lab/` | Квантовая лаборатория: brain, okx-боты, эволюция, 8 агентов | [palcikia-crypto/quantum_lab](https://github.com/palcikia-crypto/quantum_lab) |
| `aegis-lab/` | AEGIS-Lab v3 PRO: paper-движок 1:1, fee-edge, Actions-воркер | [palcikia-crypto/aegis-lab](https://github.com/palcikia-crypto/aegis-lab) |
| `pseudo-opt-bot/` | Скальпер Bybit perps (postOnly, RR 2.5:1) + panel/ | [palcikia-crypto/pseudo-opt-bot](https://github.com/palcikia-crypto/pseudo-opt-bot) |
| `scalp-lab-pro-/` | OmniLab: Polymarket live + scalp backtests + OKX | [palcikia-crypto/scalp-lab-pro-](https://github.com/palcikia-crypto/scalp-lab-pro-) |
| `scalp-lab-adaptive/` | Скальп-лаба: brain, allocator, reality_check | [palcikia-crypto/scalp-lab-adaptive](https://github.com/palcikia-crypto/scalp-lab-adaptive) |
| `scalp-lab-termux/` | Скальп-бэктесты 30+ стратегий + paper + дашборд | [palcikia-crypto/scalp-lab-termux](https://github.com/palcikia-crypto/scalp-lab-termux) |
| `polymarket-lab/` | Polymarket Crypto Lab (scan/arbitrage/dashboard) | [palcikia-crypto/polymarket-lab](https://github.com/palcikia-crypto/polymarket-lab) |
| `ai-arb-bot/` | Gift-card арбитраж: сканер + разовые скрипты | [palcikia-crypto/ai-arb-bot](https://github.com/palcikia-crypto/ai-arb-bot) |
| `vphone-hub/` | Android: менеджер ВМ, терминал, ИИ, почта, автоматизации | [palcikia-crypto/vphone-hub](https://github.com/palcikia-crypto/vphone-hub) |
| `pseudo-options-lab/` | Заготовка (пустой) | [palcikia-crypto/pseudo-options-lab](https://github.com/palcikia-crypto/pseudo-options-lab) |

## Известное состояние копий (позаимствовано из аудита)

- `scalp-lab-pro-` — БИТЫЙ: worker/reporter/paper падают с `SyntaxError`.
  Чистые живые копии тех же модулей: `scalp-lab-termux/scalp-lab/`.
- `polymarket-lab` — 15 файлов с SyntaxError, включая `autolab/lab.py` (CI маскирует падения через `|| true`).
- `pseudo-opt-bot` — закоммичен `venv/` (206 МБ), стоит вычистить.

## Важно про GitHub Actions

Воркспейсы в подпапках (`*/.github/workflows/*.yml`) **не запускаются** из монорепо —
Actions читает только корневой `.github/workflows/`. При улучшении рабочие пайплайны
подлежит перенести в корень с `working-directory:` (плюс переименовать, чтобы cron-ы разных
лаб не конфликтовали).
