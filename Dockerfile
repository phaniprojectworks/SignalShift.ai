FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python scripts/generate_data.py --reset
EXPOSE 8080

CMD test -f /mnt/storage/signalshift.db || (python scripts/generate_data.py --reset && mv signalshift.db /mnt/storage/signalshift.db); ln -sf /mnt/storage/signalshift.db /app/signalshift.db && python run.py
