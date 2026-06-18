# Alice Code — порт на TypeScript

Экспериментальный порт проекта (ветка `js-ts-port`) с Python на **Node.js + TypeScript**.
Полностью повторяет архитектуру Python-версии; в корне репозитория остаётся оригинал на Python.

## Требования
- Node.js 20+ (проверено на v24)

## Установка и запуск
```bash
cd js
npm install
npx playwright install chromium   # один раз, для входа в Яндекс
npm start                          # поднимет адаптер и REPL
```
Команды в чате те же: `/resume`, `/clear`, `/trust [all|danger|none]`, `/help`, `/exit`.

## Тесты и типы
```bash
npm test          # vitest — юнит-тесты логики (23 теста)
npm run typecheck # tsc --noEmit
```

## Структура
| Файл | Аналог в Python | Назначение |
|------|-----------------|-----------|
| `src/protocol.ts` | часть `alice_adapter.py` | рендер промпта + разбор tool_call (тела `@@…@@`, толерантный JSON, B3) |
| `src/tools.ts` | часть `agent.py` | инструменты read/write/edit/list/glob/grep/run + песочница |
| `src/trust.ts` | часть `agent.py` | уровень доверия / подтверждения |
| `src/sessions.ts` | часть `agent.py` | сессии (один dialog_id, транскрипт на диске) |
| `src/alice.ts` | `AliceClient` | WebSocket-клиент uniproxy |
| `src/session-capture.ts` | `alice_session.py` | автологин через Playwright |
| `src/adapter.ts` | FastAPI-часть | локальный OpenAI-совместимый HTTP-сервер |
| `src/agent.ts` | `agent.py` | REPL-агент |

В отличие от Python, адаптер поднимается **в том же процессе** (без отдельного subprocess).

## Что покрыто проверкой
- Юнит-тесты (vitest) — протокол tool_call, инструменты grep/glob + песочница, доверие, сессии — **23/23**.
- `tsc --noEmit` — весь проект без ошибок типов.
- Дымовой тест адаптера — `/health` и `/v1/models` отвечают.

## Известные отличия (паритет не 100%)
- **Многострочная вставка (Ctrl+V):** Python-версия использует `prompt_toolkit` (bracketed paste). Здесь ввод на `readline` — многострочная вставка может разбиваться построчно. TODO для полного паритета.
- Живые вызовы к Алисе и интерактивный вход не покрыты автотестами (как и в Python-версии) — проверяются вручную запуском.
