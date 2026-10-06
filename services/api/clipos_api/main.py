"""FastAPI application entrypoint."""

import os
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException

from clipos_api.operator import OperatorSnapshot
from clipos_api.repository import get_engine

try:
    APP_VERSION = version("clipos-api")
except PackageNotFoundError:
    APP_VERSION = "0.0.0"

app = FastAPI(title="ClipOS API", version=APP_VERSION)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a machine-readable process health payload."""
    return {"status": "ok", "version": APP_VERSION}


def database_engine() -> sa.Engine:
    """Return the process-cached engine configured by ``DATABASE_URL``."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        return get_engine(url)
    except sa.exc.ArgumentError as exc:
        raise HTTPException(status_code=503, detail="database configuration invalid") from exc


@app.get("/operator/health")
def operator_health(engine: Annotated[sa.Engine, Depends(database_engine)]) -> OperatorSnapshot:
    """Return the read-only operator snapshot; Temporal reachability is caller-supplied."""
    return OperatorSnapshot.build(
        engine,
        datetime.now(UTC),
        temporal_reachable=bool(os.environ.get("TEMPORAL_ADDRESS")),
    )
