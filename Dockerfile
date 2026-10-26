# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Install runtime dependencies first so the layer is cached.
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-audit --no-fund -r requirements.txt

# Application code.
COPY server.py ./
COPY templates ./templates
COPY static ./static
COPY README.md LICENSE ./

# The database lives in a dedicated, volume-mounted directory.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 --gid 10001 appuser && \
    chown -R appuser:appuser /app

USER appuser

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=2).status==200 else 1)"

CMD ["sh", "-c", "python server.py --host 0.0.0.0 --port ${PORT}"]
