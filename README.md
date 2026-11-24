# mksn-logs

Приватний бот-логер: слідкує за YouTube, TikTok і Twitch та постить нові
відео/стріми в Telegram-канал.

Працює на GitHub Actions. Щогодинний запуск ініціює Google Apps Script
(через `workflow_dispatch`); GitHub-розклад (cron) вимкнено.

## Що робить

- 3 YouTube-канали (рідний YouTube RSS, без API-ключа)
- TikTok (через RSSHub-фід)
- Twitch VOD + Twitch LIVE (через RSSHub-фіди)
- щогодини перевіряє фіди (зовнішній тригер Google Apps Script → `workflow_dispatch`)
- дедуплікація через `state.json` (зберігається в Actions cache)
- прев'ю-картинка + назва + посилання; відео не завантажується

## Налаштування

1. Створити бота в [@BotFather](https://t.me/BotFather) (`/newbot`) і скопіювати токен.
2. Додати бота адміністратором у канал з правом «Публікація повідомлень».
3. У репозиторії: **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN` — токен бота
   - `TELEGRAM_CHAT_ID` — `@username_каналу` або числовий ID `-100...`
4. Вкладка **Actions → logs → Run workflow** — перший запуск.

Перший запуск лише запам'ятовує поточний стан і постить найновіший запис з
кожного джерела (щоб одразу було видно, що все працює).

## Змінити джерела

`sources.json` — список об'єктів:

```json
{ "id": "yt-main", "name": "mksn", "type": "youtube", "url": "https://..." }
```

`type` впливає лише на емодзі та підпис: `youtube`, `tiktok`, `twitch_vod`,
`twitch_live`. Щоб вимкнути джерело — прибрати його зі списку.

## Локальний запуск

```bash
python bot.py --dry-run           # показати, що бот запостив би
```

Для реального тесту:

```bash
set TELEGRAM_BOT_TOKEN=...
set TELEGRAM_CHAT_ID=@your_channel
python bot.py --test
```

## Приватність

- Репозиторій публічний (щоб GitHub Actions були безкоштовними); секретів у коді немає.
- Токен і chat ID зберігаються лише в GitHub Secrets (не в коді).
- Токен планувальника зберігається в Script Properties Google Apps Script, не в коді.
- `state.json` і `.env` у `.gitignore`.
- Коміти робляться з GitHub noreply-адресою, особиста пошта не потрапляє в історію.

## Обмеження

- Запуск ініціює Google Apps Script щогодини. GitHub-розклад (cron) вимкнено,
  бо в цьому акаунті він нестабільний і іноді створює дублікати.
- TikTok і Twitch-фіди йдуть через публічний RSSHub (`rsshub.rss.tips`). Якщо
  він недоступний — ці джерела тимчасово пропускаються, YouTube працює завжди.
- Репозиторій публічний, тому GitHub Actions безкоштовні; ліміт $12 не витрачається.
- Кожен запуск обмежений: HTTP-таймаут 8 с, загальний бюджет часу 45 с,
  максимум 10 повідомлень за запуск. Це гарантує, що job вкладається в одну
  білінгову хвилину навіть якщо якийсь фід завис.
