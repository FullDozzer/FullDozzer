FROM python:3.12-bookworm

# Не создавать .pyc
ENV PYTHONDONTWRITEBYTECODE=1

# Логи сразу выводятся в консоль
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Системные зависимости и Python-пакеты
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

# Копируем приложение
COPY bot.py .

# Папка для постоянных данных
RUN mkdir -p /app/data /app/data/images

# Запуск
CMD ["python", "-u", "bot.py"]
