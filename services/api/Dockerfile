FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY clipos_api ./clipos_api

RUN pip install --no-cache-dir -e .

RUN useradd --create-home --uid 10001 clipos
USER clipos

EXPOSE 8000

CMD ["uvicorn", "clipos_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
