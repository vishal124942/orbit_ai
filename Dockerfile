# Use a slim Python image base because it's lighter than full Ubuntu
FROM python:3.10-slim

# Prevent React/Node interactive prompts during install
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV WHATSAPP_BRIDGE_PATH=/app/backend/src/whatsapp/gateway_v3.js

# Install system dependencies (Node.js 18, ffmpeg for audio, build tools)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    git \
    ffmpeg \
    build-essential \
    libpq-dev \
    sqlite3 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g pm2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# 1. Install Python dependencies first (for better caching)
COPY backend/requirements.txt /app/backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# 2. Install Node.js dependencies for WhatsApp bridge
COPY backend/src/whatsapp/package*.json /app/backend/src/whatsapp/
RUN cd backend/src/whatsapp && npm install --omit=dev

# 3. Copy the rest of the application code
# Assuming the root of the repo is being built
COPY backend /app/backend

# Ensure data directory exists
RUN mkdir -p /app/data/users

# Configure PM2 logs to output to stdout for Back4App logs dashboard
# We don't use the daemonizing node script here, we launch Uvicorn directly via Docker CMD
EXPOSE 8000

# Run Uvicorn directly, respecting the dynamic $PORT assigned by Back4App
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
