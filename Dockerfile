# Multi-stage build for smaller final image
FROM python:3.13-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY ./ .
RUN uv sync

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8080/', timeout=2)" || exit 1

# Run with gunicorn for production
# Threaded workers so one client slowly downloading a large export does not
# block every other request. The timeout must cover the whole response
# write, not just map generation.
CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:8080", \
     "--worker-class", "gthread", "--workers", "2", "--threads", "8", \
     "--timeout", "180", "--graceful-timeout", "30", "app:app"]
