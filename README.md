# grock_digest

Дайджест Twitter в Telegram-канал `@grock` 2 раза в день через Grok x_search.

## Деплой

1. Создать репо `BRKME/grock_digest`, запушить этот код.
2. В **Settings → Secrets and variables → Actions** добавить:
   - `XAI_API_KEY` — ключ с console.x.ai
   - `TELEGRAM_BOT_TOKEN` — твой бот, добавленный админом в `@grock`
   - `TELEGRAM_CHANNEL_ID` — `@grock` (или числовой `-100...`)
   - `TELEGRAM_OWNER_CHAT_ID` — твой chat_id для алертов (не обязателен)
3. В **Settings → Actions → General → Workflow permissions** включить **Read and write**.
4. Запустить вручную: вкладка **Actions → digest → Run workflow → morning**. Если ок — крон возьмёт оттуда.

## Расписание

- 06:00 UTC = 09:00 MSK — утро
- 18:00 UTC = 21:00 MSK — вечер

## Локальный smoke-test

```bash
pip install -r requirements.txt
export XAI_API_KEY=xai-...
python -m scripts.dryrun
```

Без обращения к Telegram, печатает JSON и итоговые сообщения в stdout.

## Стоимость

Ожидаемо ~$0.07 за выпуск, ~$4–5 в месяц. Точные числа смотри в `telemetry.jsonl`
(коммитится после каждого запуска).

## Если что-то ломается

1. Открыть последний failed run в **Actions** — там traceback.
2. Если упало на API — алерт прилетит в `TELEGRAM_OWNER_CHAT_ID`.
3. Состояние (`state.json`) и логи (`telemetry.jsonl`) лежат в репо, можно откатить
   `git revert` если нужно повторить выпуск.

## Модели

xAI ретайрит `grok-4` и `grok-4-fast` 15.05.2026. Дефолты в workflow:
- primary: `grok-4.3`
- fallback: `grok-4.20-non-reasoning`

Поменять на новые версии — отредактировать env в `.github/workflows/digest.yml`.
