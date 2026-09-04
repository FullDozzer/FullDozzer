FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY fonts ./fonts

RUN mkdir -p /app/data
RUN mkdir -p /app/data/images

# Проверяем наличие шрифтов во время сборки
RUN test -f /app/fonts/DejaVuSans.ttf
RUN test -f /app/fonts/DejaVuSans-Bold.ttf

CMD ["python", "-u", "bot.py"]
