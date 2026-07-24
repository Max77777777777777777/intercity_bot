FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/app/data
RUN mkdir -p /app/data && chmod 777 /app/data

# Запускаем bot.py напрямую — БЕЗ автообёртки Bothost (http_wrapper.py),
# т.к. bot.py уже сам поднимает и Telegram-бота, и веб-API на порту из $PORT.
CMD ["python", "bot.py"]
