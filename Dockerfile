FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY app/ app/
COPY mcp_servers/ mcp_servers/
COPY data/ data/
COPY evals/ evals/
RUN pip install --no-cache-dir '.[integrations]'
COPY --from=frontend /build/dist frontend/dist
ENV ECOMMERCE_DATA_DIR=/state
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
