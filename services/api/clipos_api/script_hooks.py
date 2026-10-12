"""Hooks, scripts, and script versioning (CLIP-048).

M11 creative-asset layer from PRD domain E: ``hooks`` are attention lines
for a brief (idempotent on the semantic key
``(org_id, brief_id, text)`` — same hook text for the same brief is the
same hook), ``scripts`` are versioned bodies grounded in one content brief,
and ``script_versions`` is an append-only content history: every save mints
the next version, identical body re-saves are no-ops, and the script row
tracks ``current_version``. All registration validates the linked brief
exists (404). Audits: ``hooks.registered``, ``scripts.registered``,
``scripts.version_appended``. No LLM, no network, no provider
identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.content_planning import ContentBriefRecord
from clipos_api.repository import Base


class HookCreate(BaseModel):
    """Request body for registering one hook (semantic idempotency)."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=512)


class Hook(BaseModel):
    """One persisted hook for one brief."""

    model_config = ConfigDict(frozen=True)

    hook_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=512)
    created_at: datetime | None = None


class Script(BaseModel):
    """One versioned script; natural key = (org_id, script_id)."""

    model_config = ConfigDict(frozen=True)

    script_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    current_version: int = Field(default=0, ge=0)
    created_at: datetime | None = None


class ScriptVersionRequest(BaseModel):
    """Request body for appending one script version."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    body: str = Field(min_length=1, max_length=8192)
    author_ref: str = Field(min_length=1, max_length=255)


class ScriptVersion(BaseModel):
    """One append-only script body revision."""

    model_config = ConfigDict(frozen=True)

    version_id: str = Field(min_length=1, max_length=128)
    script_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    body: str = Field(min_length=1, max_length=8192)
    author_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class HookRecord(Base):
    """SQL row for one hook."""

    __tablename__ = "hooks"
    __table_args__ = (
        sa.Index("uq_hooks_org_brief_text", "org_id", "brief_id", "text", unique=True),
        sa.Index("ix_hooks_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    hook_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    text: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ScriptRecord(Base):
    """SQL row for one script identity."""

    __tablename__ = "scripts"
    __table_args__ = (
        sa.Index("uq_scripts_org_script", "org_id", "script_id", unique=True),
        sa.Index("ix_scripts_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    script_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    current_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ScriptVersionRecord(Base):
    """SQL row for one append-only script version."""

    __tablename__ = "script_versions"
    __table_args__ = (
        sa.Index(
            "uq_script_versions_org_script_version",
            "org_id",
            "script_id",
            "version",
            unique=True,
        ),
        sa.Index("ix_script_versions_org_script", "org_id", "script_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    script_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    body: Mapped[str] = mapped_column(sa.String(8192), nullable=False)
    author_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _hook_from_row(row: HookRecord) -> Hook:
    return Hook(
        hook_id=row.hook_id,
        org_id=row.org_id,
        brief_id=row.brief_id,
        text=row.text,
        created_at=_aware(row.created_at),
    )


def _script_from_row(row: ScriptRecord) -> Script:
    return Script(
        script_id=row.script_id,
        org_id=row.org_id,
        brief_id=row.brief_id,
        title=row.title,
        current_version=row.current_version,
        created_at=_aware(row.created_at),
    )


def _version_from_row(row: ScriptVersionRecord) -> ScriptVersion:
    return ScriptVersion(
        version_id=row.version_id,
        script_id=row.script_id,
        org_id=row.org_id,
        version=row.version,
        body=row.body,
        author_ref=row.author_ref,
        created_at=_aware(row.created_at),
    )


class ScriptingStore:
    """Typed persistence for hooks, scripts, and script versions."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ScriptingStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def _brief(self, session: Session, brief_id: str, org_id: str) -> ContentBriefRecord:
        brief = session.scalars(
            sa.select(ContentBriefRecord).where(
                ContentBriefRecord.brief_id == brief_id,
                ContentBriefRecord.org_id == org_id,
            )
        ).first()
        if brief is None:
            raise ValueError(f"content brief not found: {brief_id}")
        return brief

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    def register_hook(self, request: HookCreate) -> Hook:
        """Idempotent register on the semantic key (org, brief, text)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            self._brief(session, request.brief_id, request.org_id)
            existing = session.scalars(
                sa.select(HookRecord).where(
                    HookRecord.org_id == request.org_id,
                    HookRecord.brief_id == request.brief_id,
                    HookRecord.text == request.text,
                )
            ).first()
            if existing is not None:
                return _hook_from_row(existing)
            row = HookRecord(
                hook_id=str(uuid4())[:12],
                org_id=request.org_id,
                brief_id=request.brief_id,
                text=request.text,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "hooks.registered",
                "hooks",
                f"hook:{row.hook_id}",
                {
                    "org_id": request.org_id,
                    "brief_id": request.brief_id,
                    "text_prefix": request.text[:64],
                },
            )
            return _hook_from_row(row)

    def list_hooks(self, org_id: str, brief_id: str) -> list[Hook]:
        with self._sessions() as session:
            self._brief(session, brief_id, org_id)
            rows = session.scalars(
                sa.select(HookRecord)
                .where(
                    HookRecord.org_id == org_id,
                    HookRecord.brief_id == brief_id,
                )
                .order_by(HookRecord.created_at, HookRecord.id)
            ).all()
            return [_hook_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------

    def register_script(self, script: Script) -> Script:
        """Idempotent register per (org, script_id); brief must exist."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            self._brief(session, script.brief_id, script.org_id)
            existing = session.scalars(
                sa.select(ScriptRecord).where(
                    ScriptRecord.org_id == script.org_id,
                    ScriptRecord.script_id == script.script_id,
                )
            ).first()
            if existing is not None:
                stored = _script_from_row(existing)
                if stored.brief_id != script.brief_id or stored.title != script.title:
                    raise ValueError(
                        f"script already registered with different fields: {script.script_id}"
                    )
                return stored
            row = ScriptRecord(
                script_id=script.script_id,
                org_id=script.org_id,
                brief_id=script.brief_id,
                title=script.title,
                current_version=0,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "scripts.registered",
                "scripts",
                f"script:{script.script_id}",
                {
                    "org_id": script.org_id,
                    "brief_id": script.brief_id,
                    "title": script.title,
                },
            )
            return _script_from_row(row)

    def get_script(self, script_id: str, org_id: str) -> Script | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ScriptRecord).where(
                    ScriptRecord.script_id == script_id,
                    ScriptRecord.org_id == org_id,
                )
            ).first()
            return _script_from_row(row) if row is not None else None

    def list_scripts(self, org_id: str, brief_id: str) -> list[Script]:
        with self._sessions() as session:
            self._brief(session, brief_id, org_id)
            rows = session.scalars(
                sa.select(ScriptRecord)
                .where(
                    ScriptRecord.org_id == org_id,
                    ScriptRecord.brief_id == brief_id,
                )
                .order_by(ScriptRecord.created_at, ScriptRecord.id)
            ).all()
            return [_script_from_row(row) for row in rows]

    def save_version(self, script_id: str, request: ScriptVersionRequest) -> ScriptVersion:
        """Append the next script version (identical body = no-op).

        The script must exist (404). Saving when the latest body already
        equals the request returns that version untouched; a changed body
        mints version N+1, bumps scripts.current_version, and audits
        scripts.version_appended.
        """
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            script = session.scalars(
                sa.select(ScriptRecord).where(
                    ScriptRecord.script_id == script_id,
                    ScriptRecord.org_id == request.org_id,
                )
            ).first()
            if script is None:
                raise ValueError(f"script not found: {script_id}")
            latest = session.scalars(
                sa.select(ScriptVersionRecord)
                .where(
                    ScriptVersionRecord.org_id == request.org_id,
                    ScriptVersionRecord.script_id == script_id,
                )
                .order_by(ScriptVersionRecord.version.desc())
            ).first()
            if latest is not None and latest.body == request.body:
                return _version_from_row(latest)
            next_version = (latest.version + 1) if latest is not None else 1
            row = ScriptVersionRecord(
                version_id=str(uuid4())[:12],
                script_id=script_id,
                org_id=request.org_id,
                version=next_version,
                body=request.body,
                author_ref=request.author_ref,
                created_at=now,
            )
            session.add(row)
            script.current_version = next_version
            session.flush()
            audit.record(
                "scripts.version_appended",
                "script_versions",
                f"script_version:{row.version_id}",
                {
                    "org_id": request.org_id,
                    "script_id": script_id,
                    "version": next_version,
                    "author_ref": request.author_ref,
                },
            )
            return _version_from_row(row)

    def list_versions(self, script_id: str, org_id: str) -> list[ScriptVersion]:
        with self._sessions() as session:
            script = session.scalars(
                sa.select(ScriptRecord).where(
                    ScriptRecord.script_id == script_id,
                    ScriptRecord.org_id == org_id,
                )
            ).first()
            if script is None:
                raise ValueError(f"script not found: {script_id}")
            rows = session.scalars(
                sa.select(ScriptVersionRecord)
                .where(
                    ScriptVersionRecord.org_id == org_id,
                    ScriptVersionRecord.script_id == script_id,
                )
                .order_by(ScriptVersionRecord.version)
            ).all()
            return [_version_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["scripting"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ScriptingStore:
    return ScriptingStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "already" in message:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=message)


@router.post(
    "/hooks",
    response_model=Hook,
    name="register_hook",
)
def register_hook_route(
    request: HookCreate,
    store: Annotated[ScriptingStore, Depends(_store)],
) -> Hook:
    """Register one hook (idempotent per org + brief + text)."""
    try:
        return store.register_hook(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/hooks",
    response_model=list[Hook],
    name="list_hooks",
)
def list_hooks_route(
    store: Annotated[ScriptingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    brief_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[Hook]:
    """List hooks for one brief (404 when the brief is unknown)."""
    try:
        return store.list_hooks(org_id, brief_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/scripts",
    response_model=Script,
    name="register_script",
)
def register_script_route(
    request: Script,
    store: Annotated[ScriptingStore, Depends(_store)],
) -> Script:
    """Register one script identity (idempotent per org + script id)."""
    try:
        return store.register_script(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/scripts/{script_id}",
    response_model=Script,
    name="get_script",
)
def get_script_route(
    script_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ScriptingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Script:
    """Get one script by id and org."""
    stored = store.get_script(script_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"script not found: {script_id}")
    return stored


@router.get(
    "/scripts",
    response_model=list[Script],
    name="list_scripts",
)
def list_scripts_route(
    store: Annotated[ScriptingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    brief_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[Script]:
    """List scripts for one brief (404 when the brief is unknown)."""
    try:
        return store.list_scripts(org_id, brief_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/scripts/{script_id}/versions",
    response_model=ScriptVersion,
    name="save_script_version",
)
def save_script_version_route(
    request: ScriptVersionRequest,
    script_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ScriptingStore, Depends(_store)],
) -> ScriptVersion:
    """Append the next script version (identical body returns latest)."""
    try:
        return store.save_version(script_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/scripts/{script_id}/versions",
    response_model=list[ScriptVersion],
    name="list_script_versions",
)
def list_script_versions_route(
    script_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[ScriptingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ScriptVersion]:
    """List append-only versions for one script (404 when unknown)."""
    try:
        return store.list_versions(script_id, org_id)
    except ValueError as exc:
        raise _map_error(exc) from exc
