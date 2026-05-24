FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY requirements.txt ./
COPY app ./app
COPY seed ./seed
COPY tests ./tests

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -e .[test,migration]

EXPOSE 8080

CMD ["sh", "-c", "for i in $(seq 1 60); do python -m app.ydb_schema && break; if [ \"$i\" = \"60\" ]; then exit 1; fi; sleep 2; done && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8080"]
