FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so application edits don't invalidate the install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run unprivileged. uid 1000 matches the bind-mounted source in development.
#
# /data must exist and be owned by appuser BEFORE the named volume is attached:
# Docker seeds an empty volume from the image's directory, ownership included.
# Without this the volume arrives root-owned and the order book cannot be written.
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

# Production-shaped default. docker-compose.yml overrides this with --reload
# for local development.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
