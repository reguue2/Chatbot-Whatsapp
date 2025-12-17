# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

# Install build dependencies (only in the builder stage)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory for dependency installation
WORKDIR /app

# Copy dependency list and install into /install prefix for reuse
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

# Set working directory for the application source
WORKDIR /app

# Copy Python dependencies from the builder stage
COPY --from=builder /install /usr/local

# Copy application code into the container
COPY . .

# Create and switch to a non-root user
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose the application port
EXPOSE 8000

# Health check against the readiness endpoint every 30 seconds
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import sys, urllib.request; url='http://127.0.0.1:8000/ready'; status=urllib.request.urlopen(url, timeout=3).status; sys.exit(0 if status < 400 else 1)"

# Default command using Gunicorn with Uvicorn workers
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:8000", "wsgi:application"]
