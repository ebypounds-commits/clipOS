"""FastAPI application entrypoint."""
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI

try:
    APP_VERSION = version("clipos-api")
except PackageNotFoundError:
    APP_VERSION = "0.0.0"

app = FastAPI(title="ClipOS API", version=APP_VERSION)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a machine-readable process health payload."""
    return {"status": "ok", "version": APP_VERSION}
