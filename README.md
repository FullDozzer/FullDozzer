# Telegram-бот для мониторинга расписания колледжа

## Быстрый старт (уже настроено)

Я уже встроил ваши параметры в `bot.py`, поэтому можно запустить без `.env`:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
python bot.py
```

Этот бот проверяет сайт расписания колледжа и пишет в Telegram, если расписание изменилось.

- Открывает **прямую ссылку расписания группы на завтра** (дата в URL обновляется автоматически).
> Простыми словами: бот сам открывает сайт, выбирает вашу группу, ставит дату **на завтра** и сравнивает с тем, что было раньше.

---

> ⚠️ Важно: токен бота записан в коде для удобного старта. Лучше потом перенести его в `.env` и сменить токен в BotFather.

## 1) Что нужно заранее

1. **Установить Python 3.11+**
   - Скачайте с официального сайта: https://www.python.org/downloads/
   - При установке на Windows обязательно поставьте галочку **Add Python to PATH**.

2. **Создать Telegram-бота** через `@BotFather`
   - Откройте Telegram → найдите `@BotFather`
   - Команда: `/newbot`
   - Придумайте имя и username
   - Скопируйте токен (вида `123456:ABC...`) — это `BOT_TOKEN`

3. **Написать боту в Telegram**
   - Просто отправьте ему `/start` после запуска — бот сам добавит ваш чат в подписчики.

---

## 2) Скачивание проекта

Если у вас уже есть папка проекта — пропустите этот шаг.

```bash
git clone <URL_ВАШЕГО_РЕПО>
cd FullDozzer
```

---

## 3) Установка (Linux/macOS)

Откройте терминал в папке проекта и выполните по очереди:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

---

## 4) Установка (Windows PowerShell)

Откройте **PowerShell** в папке проекта:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

Если PowerShell ругается на запуск скриптов:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

и снова активируйте `.venv`.

---

## 5) Настройка `.env`

1. Скопируйте пример:

```bash
cp .env.example .env
```

На Windows:

```powershell
copy .env.example .env
```

2. Откройте `.env` и заполните:

```env
BOT_TOKEN=ваш_токен_от_BotFather
SCHEDULE_URL=http://www.ishnk.ru/2025/site/schedule/group/508/2026-02-25
GROUP_NAME=точное_название_группы
TIMEZONE=Europe/Moscow
CHECK_INTERVAL_SECONDS=1800
DATE_FORMAT=%d.%m.%Y
IGNORE_HTTPS_ERRORS=true
```

Минимально обязательно заполнить: `BOT_TOKEN`, `SCHEDULE_URL` (ссылка на расписание группы), `GROUP_NAME`.

Поддерживается 3 формата `SCHEDULE_URL`:
- с плейсхолдером даты: `.../group/508/{date}`
- с фиксированной датой в конце: `.../group/508/2026-02-25`
- без даты в конце: `.../group/508`

Бот всё равно подставит **завтрашнюю дату** автоматически.

---

## 6) Запуск бота

### Linux/macOS
```bash
source .venv/bin/activate
python bot.py
```

### Windows PowerShell
```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

Если всё хорошо — бот запустится и начнёт проверять расписание.

После команды `/start` ваш чат автоматически попадает в список подписчиков, и бот будет присылать обновления всем подписанным пользователям.

---

## 7) Проверка, что работает

1. Напишите боту в Telegram: `/start`
2. Затем: `/checknow`
3. Бот ответит:
   - отправит текущее расписание (первый запуск)
   - или `Изменений не обнаружено.`
   - или отправит **новое расписание** при изменении

---

## Частые проблемы

### 1. `Не нашёл группу ...`
Значит `GROUP_NAME` не совпадает с тем, как группа называется на сайте. Скопируйте название группы **буква в букву**.

### 2. Ошибка браузера Playwright
Переустановите браузер:

```bash
python -m playwright install chromium
```

### 3. Ошибка `Не найдены обязательные переменные окружения` или `KeyError: BOT_TOKEN`
- Создайте файл `.env` рядом с `bot.py` (командой `copy .env.example .env` или `cp .env.example .env`)
- Заполните минимум: `BOT_TOKEN`, `SCHEDULE_URL`, `GROUP_NAME`
- Запускайте `python bot.py` из папки проекта

### 4. Бот молчит
- Проверьте `BOT_TOKEN`
- Убедитесь, что вы написали боту хотя бы 1 сообщение


### 5. Ошибка `ERR_CERT_AUTHORITY_INVALID`
Это проблема SSL-сертификата сайта (часто на старых серверах).
- В `.env` поставьте: `IGNORE_HTTPS_ERRORS=true`
- В текущей версии это уже включено по умолчанию.

---

## Команды бота
- `/start` — показать статус и подписаться на уведомления
- `/subscribe` — подписаться на рассылку
- `/unsubscribe` — отписаться от рассылки
- `/checknow` — немедленно проверить расписание

---

## Для вашего сайта может понадобиться донастройка

У колледжей разные сайты расписания. Если структура сайта нестандартная, нужно будет поправить селекторы в функции `fetch_schedule_text()` в `bot.py`.
