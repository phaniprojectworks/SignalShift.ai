# Use official lightweight Python 3.11 runtime

FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr for Cloud Run logs

ENV PYTHONDONTWRITEBYTECODE=1 

PYTHONUNBUFFERED=1 

PORT=8080

WORKDIR /app

# Install dependencies first for faster layer caching

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code

COPY . .

# Expose default Cloud Run port

EXPOSE 8080

# 1. If /mnt/storage/signalshift.db does not exist yet in the mounted Cloud Storage bucket,

# build the initial SQLite database on local container disk and move it into the bucket.

# 2. Link /app/signalshift.db -> /mnt/storage/signalshift.db so ./signalshift.db persists.

# 3. Start the Uvicorn server via run.py.

CMD ["sh", "-c", "if [ ! -f /mnt/storage/signalshift.db ]; then python scripts/generate_data.py --reset && mv signalshift.db /mnt/storage/signalshift.db; fi && ln -sf /mnt/storage/signalshift.db /app/signalshift.db && python run.py"]
