# Multi-stage production Dockerfile for Crypto Arbitrage System

# --- Stage 1: Build & Dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Production Runtime ---
FROM python:3.12-slim AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root system user
RUN groupadd -g 1000 arbgroup && \
    useradd -m -u 1000 -g arbgroup arbuser

# Copy virtual environment from builder
COPY --from=builder --chown=arbuser:arbgroup /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy application source code and configuration
COPY --chown=arbuser:arbgroup . /app

# Switch to non-root execution
USER arbuser

# Control plane (FastAPI) and Dashboard (Streamlit) ports
EXPOSE 8000 8501

# Default entrypoint runs the autonomous trading pipeline
CMD ["python", "-m", "src.main"]
