FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY clipos_api ./clipos_api

RUN pip install --no-cache-dir -e .

# ffmpeg is required by the mock video/adapters for media rendering; keep the
# image self-contained so the qualification harness exercises the stage
# instead of skipping it (and CI stays green under --strict).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 clipos
# The audit log is written under the working directory; the API runs as
# this unprivileged user, so the directory has to exist and be owned by it.
RUN mkdir -p /app/.agent/audit && chown -R clipos:clipos /app/.agent
USER clipos

EXPOSE 8000

CMD ["uvicorn", "clipos_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
