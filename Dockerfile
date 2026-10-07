FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8501

# DKUBEX_BASE_PATH is injected by the DKubeX platform when this image is deployed via the
# Helm chart in charts/feedback-pulse; it is empty (and --server.baseUrlPath is a no-op) for
# plain local/docker-compose runs. Shell form so the env var is expanded at container start.
CMD streamlit run app.py --server.port=8501 --server.address=0.0.0.0 \
    --server.baseUrlPath="${DKUBEX_BASE_PATH:-}"
