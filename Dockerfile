FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# Шрифты с поддержкой кириллицы + сертификаты
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu \
        fonts-noto-core \
        fontconfig \
        ca-certificates \
    && fc-cache -f -v \
    && echo "=== INSTALLED CYRILLIC FONTS ===" \
    && fc-list :lang=ru family file \
    && echo "=================================" \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN mkdir -p /app/data/images

# Финальная проверка: если шрифта нет — Docker build остановится
RUN test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    || test -f /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
    || test -f /usr/share/fonts/truetype/noto/NotoSans-Regular.ttf

CMD ["python", "-u", "bot.py"]
