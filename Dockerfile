# MADA agents + live dashboard
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcairo2 procps \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mada ./mada
COPY demo ./demo
COPY run_experiments.py .
ENV PYTHONUNBUFFERED=1 PGTZ=UTC
EXPOSE 8000
CMD ["python", "-m", "demo.server"]
