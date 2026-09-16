FROM python:3.12-slim

# Run as a dedicated, unprivileged user rather than root - if anything in
# this app or its dependencies is ever compromised, it has no elevated
# access to the container or host. UID 1000 is used because it matches the
# default first user on most Linux hosts, which keeps bind-mount
# permissions (./data) simple - see README for what to do if your host
# user has a different UID.
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /usr/sbin/nologin --no-create-home appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY init_db.py .

# data/ is where the SQLite database and uploaded receipts live -
# mount this as a volume so your data survives container restarts/rebuilds.
RUN mkdir -p /app/data/uploads /app/data/backups && \
    chown -R appuser:appuser /app
VOLUME ["/app/data"]

# No .pyc writes needed (avoids requiring write access to /app, so the
# image works with a read-only root filesystem - see docker-compose.yml).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
