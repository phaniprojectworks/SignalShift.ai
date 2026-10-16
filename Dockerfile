FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Litestream: optional continuous replication of the SQLite database to Cloud Storage.
# Installed always, used only when LITESTREAM_REPLICA_URL is set (see deploy/start.sh).
ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz && rm /tmp/litestream.tar.gz && litestream version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# a Windows checkout can carry CRLF line endings, which /bin/sh cannot run
RUN sed -i 's/\r$//' deploy/start.sh

EXPOSE 8080

# Default: the database on the Cloud Storage volume at /mnt/storage, exactly as before.
CMD ["sh", "deploy/start.sh"]
