# 🔍 TRON & BEP-20 Vanity Address Telegram Bot

Telegram-бот для генерации ванити адресов TRON (TRC-20) и BNB Chain (BEP-20).

## Возможности

- 🔴 **TRON (TRC-20)** — поиск адресов с нужным префиксом после `T` (~1.5 млн адресов/сек)
- 🟡 **BEP-20 (BSC)** — поиск адресов с нужным HEX-префиксом после `0x` (~3–5 тыс. адресов/сек)
- 🔐 Система доступа: только авторизованные пользователи
- 👨‍💼 Управление пользователями через команды администратора
- 📊 Живой прогресс поиска в реальном времени

## Зависимости

### Python
```
python-telegram-bot >= 20.0
python-dotenv
```

### Системные (для компиляции генераторов)
- GCC, OpenSSL (dev headers), libpcre, pthreads

### vanitygen++
Для генерации TRC-20 адресов требуется скомпилированный `vanitygen++`:
```bash
git clone https://github.com/10gic/vanitygen-plusplus vanitygen-plusplus
cd vanitygen-plusplus
make
```

## Установка

1. Склонируй репозиторий:
```bash
git clone https://github.com/evgenixkr-collab/tron-bep20-vanity-bot.git
cd tron-bep20-vanity-bot
```

2. Установи Python-зависимости:
```bash
pip install -r requirements.txt
```

3. Склонируй и скомпилируй vanitygen++:
```bash
git clone https://github.com/10gic/vanitygen-plusplus vanitygen-plusplus
cd vanitygen-plusplus && make && cd ..
```

4. Скомпилируй BEP-20 генератор:
```bash
make ethvanitygen
```

5. Создай файл `.env`:
```env
TELEGRAM_BOT_TOKEN=твой_токен_от_BotFather
ADMIN_TELEGRAM_ID=твой_telegram_id
```

6. Запусти бота:
```bash
python main.py
```

## Команды администратора

| Команда | Описание |
|---------|----------|
| `/admin` | Справка по командам |
| `/adduser <id> [имя]` | Выдать доступ пользователю |
| `/removeuser <id>` | Отозвать доступ |
| `/listusers` | Список пользователей с доступом |

## Примерное время поиска

### TRON (TRC-20) — ~1.5 млн адресов/сек
| Длина префикса | Время (50%) |
|---|---|
| 3 символа | секунды |
| 4 символа | ~10 минут |
| 5 символов | ~4 часа |

### BEP-20 — ~3–5 тыс. адресов/сек
| Длина префикса | Время (50%) |
|---|---|
| 2 символа | секунды |
| 3 символа | ~1–2 минуты |
| 4 символа | ~20–30 минут |

## Безопасность

- Приватные ключи генерируются локально и **нигде не сохраняются**
- Бот не подключается к блокчейну для поиска
- Секреты (`TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_ID`) хранятся в `.env` и не попадают в репозиторий
