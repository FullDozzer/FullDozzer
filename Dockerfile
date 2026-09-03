FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        fonts-dejavu \
        fontconfig \
        ca-certificates && \
    fc-cache -f -v && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

RUN mkdir -p /app/data/images

CMD ["python", "-u", "bot.py"]
