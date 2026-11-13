"""Adapter provenance acceptance tests (W2 / P0-2).

The audit's finding: a publication produced by a mock adapter reported
exactly what a real upload would report (status SUCCEEDED, no marker), so
an operator could not tell whether anything actually left the machine.
Every adapter-backed response now states its kind, and these tests pin the
contract: deterministic stand-ins say ``mock``, external integrations say
``real``, and a job reports the kind of the adapter its platform resolves
to - not merely the field's default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.analytics import MockAnalyticsAdapter
from clipos_api.cost_ledger import CostSyncResult, MockCostAdapter
from clipos_api.jev_tools import (
    JevAnswer,
    JevHealth,
    JevQuestion,
    JevRequest,
    JevResult,
    MockJevAdapter,
    QuestionType,
)
from clipos_api.llm_gateway import LLMResponse
from clipos_api.publishing import (
    AdapterHealth as PublishingAdapterHealth,
)
from clipos_api.publishing import (
    MockPublishingAdapter,
    PublicationJob,
    PublishPolicy,
    get_adapter,
)
from clipos_api.revenue_ledger import MockRevenueAdapter, SyncResult
from clipos_api.transcription import MockTranscriptionAdapter, TranscriptionResult
from clipos_api.voice_assets import MockTTSAdapter, VoiceSynthesisResult

# ---------------------------------------------------------------------------
# Every platform adapter currently in the tree is a deterministic stand-in
# (their own docstrings say so); they must not claim a real external effect.
# ---------------------------------------------------------------------------

STAND_IN_CLASSES = (
    MockPublishingAdapter,
    MockTTSAdapter,
    MockCostAdapter,
    MockRevenueAdapter,
    MockAnalyticsAdapter,
    MockTranscriptionAdapter,
    MockJevAdapter,
)


@pytest.mark.parametrize("cls", STAND_IN_CLASSES, ids=lambda c: c.__name__)
def test_mock_adapter_classes_declare_mock(cls) -> None:
    assert getattr(cls, "adapter_kind", None) == "mock"


def test_platform_publishing_adapters_declare_mock() -> None:
    from clipos_api.meta_adapter import MetaPublishingAdapter
    from clipos_api.tiktok_adapter import TikTokPublishingAdapter
    from clipos_api.youtube_adapter import YouTubePublishingAdapter

    for cls in (TikTokPublishingAdapter, MetaPublishingAdapter, YouTubePublishingAdapter):
        assert cls.adapter_kind == "mock", cls.__name__


def test_every_registered_platform_resolves_to_mock() -> None:
    # Importing the modules performs their at-import registration.
    from clipos_api import meta_adapter, tiktok_adapter, youtube_adapter  # noqa: F401

    for platform in ("tiktok", "meta", "youtube", "unregistered-platform", "_default"):
        adapter = get_adapter(platform)
        assert getattr(adapter, "adapter_kind", None) == "mock", platform


# ---------------------------------------------------------------------------
# Mock results carry their kind at construction, not by default.
# ---------------------------------------------------------------------------


def test_mock_transcription_result_reports_mock() -> None:
    result = MockTranscriptionAdapter().transcribe("storage://clip.mp4", "en")
    assert isinstance(result, TranscriptionResult)
    assert result.adapter_kind == "mock"


def test_mock_analytics_health_reports_mock() -> None:
    health = MockAnalyticsAdapter().health_check("acct-1")
    assert health.adapter_kind == "mock"


def test_mock_jev_result_and_health_report_mock() -> None:
    adapter = MockJevAdapter()
    health = adapter.health_check()
    assert isinstance(health, JevHealth)
    assert health.adapter_kind == "mock"

    request = JevRequest(
        org_id="org-1",
        state="The shipment arrived broken.",
        questions={
            "team": JevQuestion(
                type=QuestionType.CHOICE,
                instructions="Which team handles this?",
                criteria={"shipping": "Delivery", "billing": "Charges"},
            ),
        },
    )
    result = adapter.answer(request)
    assert isinstance(result, JevResult)
    assert result.adapter_kind == "mock"
    assert result.answers, "the mock must return at least one answer"
    for answer in result.answers:
        assert isinstance(answer, JevAnswer)
        assert answer.adapter_kind == "mock"


# ---------------------------------------------------------------------------
# Publication jobs: the headline defect. A job reports the kind of the
# adapter its platform resolves to - derived at read time, so the stored
# job cannot claim a real upload for a mock one.
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Engine:
    e = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    from clipos_api.repository import Base

    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-w2-provenance-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture()
def client(engine: Engine, audit_log: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from clipos_api import main as main_mod

    monkeypatch.setattr(main_mod, "database_engine", lambda: engine)
    return TestClient(main_mod.app)


def test_policy_reports_the_kind_of_its_platform_adapter(client: TestClient) -> None:
    response = client.post(
        "/publish-policies",
        json={
            "policy_id": "prov-policy",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
    )
    assert response.status_code in (200, 409), response.text
    assert response.json()["adapter_kind"] == "mock"


def test_publication_job_reports_mock_adapter_kind(client: TestClient, clear_rights) -> None:
    """POST and GET must agree, and both must say mock for a mock upload."""
    client.post(
        "/publish-policies",
        json={
            "policy_id": "prov-policy",
            "org_id": "org-1",
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
    )
    clear_rights("storage://prov-item")
    submitted = client.post(
        "/publications",
        json={
            "org_id": "org-1",
            "platform": "youtube",
            "account_ref": "yt://main",
            "content_ref": "storage://prov-item",
            "title": "Provenance probe",
            "visibility": "PRIVATE",
            "idempotency_key": "w2-prov-key-1",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["status"] == "SUCCEEDED", body
    assert body["adapter_kind"] == "mock", body

    fetched = client.get(f"/publications/{body['job_id']}", params={"org_id": "org-1"})
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["adapter_kind"] == "mock"


# ---------------------------------------------------------------------------
# The field itself: present, additive, and defaulting to the honest value
# for anything that has not declared itself.
# ---------------------------------------------------------------------------


RESPONSE_MODELS = (
    PublicationJob,
    PublishPolicy,
    PublishingAdapterHealth,
    TranscriptionResult,
    VoiceSynthesisResult,
    CostSyncResult,
    SyncResult,
    JevResult,
    JevHealth,
    JevAnswer,
    LLMResponse,
)


@pytest.mark.parametrize("model", RESPONSE_MODELS, ids=lambda m: m.__name__)
def test_response_model_exposes_adapter_kind(model) -> None:
    assert "adapter_kind" in model.model_fields, model.__name__
    assert model.model_fields["adapter_kind"].default == "real"


def test_openapi_exposes_adapter_kind_on_the_job() -> None:
    from clipos_api.main import app

    schema = app.openapi()["components"]["schemas"]["PublicationJob"]
    field = schema["properties"]["adapter_kind"]
    assert field["enum"] == ["mock", "real"], field
