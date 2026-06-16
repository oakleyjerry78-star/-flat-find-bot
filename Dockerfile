# Базовий образ із вже налаштованим Playwright + Chromium
FROM mcr.microsoft.com/playwright/python:v1.52.0-jammy

WORKDIR /app

# Встановлюємо системні пакети для Python wheels, якщо Railway збирає частину залежностей
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Встановлюємо залежності
COPY requirements.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Код
COPY . .

# ENV
ENV PYTHONUNBUFFERED=1

# Запуск бота
CMD ["python", "main.py"]
