FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so application edits don't invalidate the install layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY analytics ./analytics

# Run unprivileged. uid 1000 matches the bind-mounted source in development.
RUN useradd --create-home --uid 1000 appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Production-shaped default. docker-compose.yml overrides this with --reload
# for local development.
CMD ["uvicorn", "analytics.api:app", "--host", "0.0.0.0", "--port", "8000"]
