# MiniGames

Telegram-бот с мини-играми: [@minigamesisbot](https://t.me/minigamesisbot) · канал [@minigamesbottgk](https://t.me/minigamesbottgk)

- игры в личке, в группах и через инлайн-режим (`@minigamesisbot` в любом чате);
- Mini App с играми на ловкость, магазином, сезонным пропуском и ивентами;
- монеты, квесты, достижения, рейтинги;
- бот в личных переписках через Telegram Business: удалённые и изменённые сообщения, `.mute`, игры с собеседником.

## Запуск

Нужен Python 3.11+.

```bash
pip install -r requirements.txt
cp .env.example .env      # впишите TELEGRAM_TOKEN и остальное по комментариям
python bot.py
```

Mini App нужен публичный HTTPS-адрес (`WEBAPP_URL`); без него поставьте `WEBAPP_ENABLED=0`.
Данные хранятся в SQLite (`bot_data.sqlite3`), схема обновляется при запуске.

## Устройство

| Файл | Что внутри |
| --- | --- |
| `bot.py` | обработчики Telegram, игры в чатах, магазин, ивенты, админ-панель |
| `webapp.py`, `webapp/` | сервер и клиент Mini App |
| `room_games.py` | игры в пати-комнатах |
| `business_triggers.py` | игры по ключевым словам в бизнес-чатах |
| `core/` | база и миграции, ивенты, казино, логика Telegram Business |
| `ui/` | отрисовка профиля и открыток |

## Лицензия

MIT — см. [LICENSE](LICENSE).
