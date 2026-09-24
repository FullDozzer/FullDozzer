FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY staff_directory.py .
COPY schedule_icons.py .
COPY fonts ./fonts
COPY assets ./assets

RUN mkdir -p /app/data
RUN mkdir -p /app/data/images

# Проверяем наличие шрифтов во время сборки
RUN test -f /app/fonts/DejaVuSans.ttf
RUN test -f /app/fonts/DejaVuSans-Bold.ttf
RUN test -f /app/assets/icons/calendar.svg
RUN test -f /app/assets/icons/clock.svg
RUN test -f /app/assets/icons/user.svg
RUN test -f /app/assets/icons/graduation-cap.svg

CMD ["python", "-u", "bot.py"]
