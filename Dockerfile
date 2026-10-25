# ==============================================================================
# Stage 1: Build the React Glassmorphic SPA
# ==============================================================================
FROM node:22-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ==============================================================================
# Stage 2: FastAPI + Uvicorn Production Runtime
# ==============================================================================
FROM python:3.13-slim
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED="1"
ENV SERVE_REACT_SPA="1"
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend-builder /frontend/dist /app/frontend/dist

RUN mkdir -p /app/uploaded_oas /app/session_uploads /app/session_workspaces && \
    adduser --disabled-password --gecos "" myuser && \
    chown -R myuser:myuser /app

USER myuser

ENV PATH="/home/myuser/.local/bin:$PATH"
ENV PORT=8080

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-keep-alive 75"]
