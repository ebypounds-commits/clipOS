"""Platform account registry (CLIP-059 / 08_INTEGRATIONS requirement).

Stores channel/account linkage and OAuth scope metadata for external
publishing platforms (YouTube, TikTok, Meta). Accounts are
``account_ref``-keyed (the same identifier carried on
``publication_jobs``) and org-scoped. OAuth secrets live in a vault, never
in this table — only vault references. Idempotent per
``(org_id, platform, account_ref)``; conflict on changed fields.

Audits: ``platform_accounts.registered``. No provider/model identifiers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class PlatformAccount(BaseModel):
    """One publishing-platform account; natural key = (org, platform, account_ref)."""

    model_config = ConfigDict(frozen=True)

    account_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    channel_ref: str | None = Field(default=None, max_length=255)
    scopes: list[str] = Field(default_factory=list, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class PlatformAccountRecord(Base):
    """SQL row for one platform account."""

    __tablename__ = "platform_accounts"
    __table_args__ = (
        sa.Index(
            "uq_platform_accounts_org_platform_ref",
            "org_id",
            "platform",
            "account_ref",
            unique=True,
        ),
        sa.Index("ix_platform_accounts_org_platform", "org_id", "platform"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    channel_ref: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    scopes: Mapped[list] = mapped_column(sa.JSON, nullable=False, default=list)
    account_metadata: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _account_from_row(row: PlatformAccountRecord) -> PlatformAccount:
    return PlatformAccount(
        account_id=row.account_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        display_name=row.display_name,
        channel_ref=row.channel_ref,
        scopes=list(row.scopes or []),
        metadata=dict(row.account_metadata or {}),
        created_at=_aware(row.created_at),
    )


class PlatformAccountStore:
    """Typed persistence for platform accounts."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> PlatformAccountStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_account(self, account: PlatformAccount) -> PlatformAccount:
        """Idempotent per (org, platform, account_ref); conflict on change."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PlatformAccountRecord).where(
                    PlatformAccountRecord.org_id == account.org_id,
                    PlatformAccountRecord.platform == account.platform,
                    PlatformAccountRecord.account_ref == account.account_ref,
                )
            ).first()
            if existing is not None:
                stored = _account_from_row(existing)
                if (
                    stored.display_name != account.display_name
                    or stored.channel_ref != account.channel_ref
                    or stored.scopes != account.scopes
                    or stored.metadata != account.metadata
                ):
                    raise ValueError(
                        f"platform account already registered with different fields: "
                        f"{account.platform}/{account.account_ref}"
                    )
                return stored
            row = PlatformAccountRecord(
                account_id=account.account_id,
                org_id=account.org_id,
                platform=account.platform,
                account_ref=account.account_ref,
                display_name=account.display_name,
                channel_ref=account.channel_ref,
                scopes=list(account.scopes),
                account_metadata=dict(account.metadata),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "platform_accounts.registered",
                "platform_accounts",
                f"platform_account:{account.account_id}",
                {
                    "org_id": account.org_id,
                    "platform": account.platform,
                    "account_ref": account.account_ref,
                    "channel_ref": account.channel_ref,
                },
            )
            return _account_from_row(row)

    def get_account(self, account_id: str, org_id: str) -> PlatformAccount | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PlatformAccountRecord).where(
                    PlatformAccountRecord.account_id == account_id,
                    PlatformAccountRecord.org_id == org_id,
                )
            ).first()
            return _account_from_row(row) if row is not None else None

    def list_accounts(self, org_id: str, platform: str | None = None) -> list[PlatformAccount]:
        with self._sessions() as session:
            stmt = sa.select(PlatformAccountRecord).where(PlatformAccountRecord.org_id == org_id)
            if platform is not None:
                stmt = stmt.where(PlatformAccountRecord.platform == platform)
            rows = session.scalars(
                stmt.order_by(PlatformAccountRecord.created_at, PlatformAccountRecord.id)
            ).all()
            return [_account_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["platform-accounts"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> PlatformAccountStore:
    return PlatformAccountStore.from_engine(engine)


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
    "/platform-accounts",
    response_model=PlatformAccount,
    name="register_platform_account",
)
def register_platform_account_route(
    request: PlatformAccount,
    store: Annotated[PlatformAccountStore, Depends(_store)],
) -> PlatformAccount:
    """Register one platform account (idempotent; vault references only)."""
    try:
        return store.register_account(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/platform-accounts/{account_id}",
    response_model=PlatformAccount,
    name="get_platform_account",
)
def get_platform_account_route(
    account_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[PlatformAccountStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> PlatformAccount:
    """Get one platform account by id and org."""
    stored = store.get_account(account_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"platform account not found: {account_id}")
    return stored


@router.get(
    "/platform-accounts",
    response_model=list[PlatformAccount],
    name="list_platform_accounts",
)
def list_platform_accounts_route(
    store: Annotated[PlatformAccountStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    platform: Annotated[str | None, Query()] = None,
) -> list[PlatformAccount]:
    """List platform accounts for one org, optionally filtered by platform."""
    return store.list_accounts(org_id, platform)
