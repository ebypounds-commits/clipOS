"""DATABASE_URL-gated PostgreSQL tests for migration and concurrency proofs."""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; PostgreSQL outbox tests run in CI lane 5",
)


def config() -> Config:
    api_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(api_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(api_dir, "migrations"))
    cfg.set_main_option("prepend_sys_path", api_dir)
    return cfg


def engine() -> Any:
    return create_engine(os.environ["DATABASE_URL"])


def now() -> datetime:
    return datetime.now(UTC)


def test_migration_tables_and_partial_unique() -> None:
    cfg = config()
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # idempotent second run
    eng = engine()
    with eng.begin() as connection:
        # Rerun-safety: a previously failed run may have committed rows.
        connection.execute(text("DELETE FROM idempotency_records WHERE key LIKE 'pg-%'"))
        tables = {
            row[0]
            for row in connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        }
        assert {"outbox_events", "idempotency_records"} <= tables
        base_insert = (
            "INSERT INTO idempotency_records (key, scope, state, attempts, business_key,"
            " created_at, updated_at) VALUES (:key, 'testing', 'in_progress', 1,"
            " :business_key, now(), now())"
        )
        connection.execute(text(base_insert), {"key": "pg-key-owned", "business_key": None})
        connection.execute(
            text(base_insert),
            {"key": "pg-key-business", "business_key": "pg-business-1"},
        )
        # pytest.raises OUTER: IntegrityError must reach begin_nested so SQLAlchemy
        # issues ROLLBACK TO SAVEPOINT before RELEASE (psycopg3 aborts the tx otherwise).
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(base_insert),
                {"key": "pg-key-duplicate", "business_key": "pg-business-1"},
            )
        # The partial predicate permits any number of NULL business keys.
        for i in range(2):
            connection.execute(text(base_insert), {"key": f"pg-key-null-{i}", "business_key": None})
        connection.execute(text("DELETE FROM idempotency_records WHERE scope='testing'"))
    eng.dispose()


def test_skip_locked_claims_are_disjoint_and_enqueue_rolls_back() -> None:
    from clipos_api import outbox as outbox_module

    eng = engine()
    outbox_module.OutboxEventRecord.metadata.create_all(eng)
    store = outbox_module.OutboxStore.from_engine(eng)
    with eng.begin() as connection:
        # Rerun-safety: the enqueue loop commits row-by-row; a prior failed run leaves them.
        connection.execute(text("DELETE FROM outbox_events WHERE aggregate_id = 'pub-1'"))
    for i in range(8):
        with store._sessions.begin() as session:
            store.enqueue(
                session,
                outbox_module.OutboxEvent(
                    id=f"pg-evt-{i}",
                    aggregate_type="publication",
                    aggregate_id="pub-1",
                    event_type="publication.test",
                    payload={"n": i},
                    available_at=now() - timedelta(seconds=1),
                    created_at=now(),
                ),
            )
    ready = threading.Event()
    holder_results: list[list[str]] = []
    claimer_results: list[list[str]] = []

    def lock_holder() -> None:
        with store._sessions.begin() as session:
            claimed = store.claim_batch_in_transaction(session, 4, 300, now())
            holder_results.append([item.id for item in claimed])
            ready.set()
            time.sleep(0.3)

    def competing_claimer() -> None:
        ready.wait(timeout=5)
        claimed = store.claim_batch(4, 300, now())
        claimer_results.append([item.id for item in claimed])

    threads = [threading.Thread(target=lock_holder), threading.Thread(target=competing_claimer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(holder_results[0]) == 4 and len(claimer_results[0]) == 4
    assert set(holder_results[0]).isdisjoint(set(claimer_results[0]))
    assert len(set(holder_results[0]) | set(claimer_results[0])) == 8

    # The event joins the caller's transaction: rollback leaves no row.
    connection = eng.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            outbox_module.OutboxEventRecord.__table__.insert().values(
                id="rollback-evt",
                aggregate_type="publication",
                aggregate_id="pub-1",
                event_type="publication.rollback",
                payload={},
                status="pending",
                attempts=0,
                available_at=now(),
                created_at=now(),
            )
        )
        visible = connection.execute(
            text("SELECT count(*) FROM outbox_events WHERE id='rollback-evt'")
        ).scalar_one()
        assert visible == 1
    finally:
        transaction.rollback()
        connection.close()
    with eng.begin() as cleanup:
        assert (
            cleanup.execute(
                text("SELECT count(*) FROM outbox_events WHERE id='rollback-evt'")
            ).scalar_one()
            == 0
        )
        cleanup.execute(text("DELETE FROM outbox_events WHERE aggregate_id='pub-1'"))
    eng.dispose()
