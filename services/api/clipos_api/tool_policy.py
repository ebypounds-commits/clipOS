"""Execution-policy layer for tool calls: sandbox, rate limits, and audit.

Every proposed tool call passes through :class:`ToolPolicyEngine`, which composes
the tool registry's immutable capability check with an operator-declared sandbox
and an in-memory, injectable-clock rate limit.  The engine records denials and
recorded calls in the audit log without ever logging argument content.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from clipos_api import audit
from clipos_api.tool_registry import ToolRegistryStore

_DEFAULT_ACTOR = "tool-policy"


class ToolPolicyDecisionReason(str, Enum):
    """Stable denial/allow reasons for tool-policy decisions."""

    CAPABILITY_OK = "capability_ok"
    RATE_LIMITED = "rate_limited"
    SANDBOX_VIOLATION = "sandbox_violation"
    TOOL_DISABLED = "tool_disabled"
    TOOL_NOT_FOUND = "tool_not_found"
    CAPABILITY_DENIED = "capability_denied"


class ToolPolicyRequest(BaseModel):
    """Identity of one proposed tool call; never carries arguments."""

    model_config = ConfigDict(frozen=True)

    agent_id: str = Field(min_length=1, max_length=128)
    agent_version: str = Field(min_length=1, max_length=128)
    tool_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    now: datetime | None = None


class ToolPolicyDecision(BaseModel):
    """Typed allow/deny result for one proposed tool call."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: ToolPolicyDecisionReason


class SandboxPolicy(BaseModel):
    """Operator-declared execution sandbox for one agent version."""

    model_config = ConfigDict(frozen=True)

    allowed_capabilities: list[str] = Field(default_factory=list)
    network_access: bool = False
    filesystem_scopes: list[str] = Field(default_factory=list)
    max_calls_per_window: int = Field(ge=1)
    window_seconds: float = Field(default=60.0, gt=0)


class ToolPolicyEngine:
    """Capability-aware sandbox and fixed-window call-rate gate."""

    def __init__(
        self,
        tools: ToolRegistryStore,
        sandbox_for: Callable[[ToolPolicyRequest], SandboxPolicy],
    ) -> None:
        self._tools = tools
        self._sandbox_for = sandbox_for
        self._calls: dict[tuple[str, str, str], deque[datetime]] = {}

    @staticmethod
    def _audit(action: str, request: ToolPolicyRequest, reason: str) -> None:
        audit.record(
            action=action,
            actor=_DEFAULT_ACTOR,
            subject=f"agent:{request.agent_id}:{request.agent_version}",
            details={
                "tool_id": request.tool_id,
                "execution_id": request.execution_id,
                "reason": reason,
            },
        )

    def _now(self, request: ToolPolicyRequest) -> datetime:
        now = request.now or datetime.now(UTC)
        return now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    def _within_sandbox(
        self, tool_capabilities: list[str], tool_metadata: dict[str, object], sandbox: SandboxPolicy
    ) -> str | None:
        if not set(tool_capabilities) <= set(sandbox.allowed_capabilities):
            return "tool capability scope outside sandbox"
        requested_network = bool(tool_metadata.get("network_access", False))
        if requested_network and not sandbox.network_access:
            return "tool requires network access"
        requested_paths = tool_metadata.get("filesystem_scopes", [])
        if not isinstance(requested_paths, list):
            return "tool filesystem scope declaration is invalid"
        if not set(requested_paths) <= set(sandbox.filesystem_scopes):
            return "tool filesystem scope outside sandbox"
        return None

    def _prune(
        self, key: tuple[str, str, str], now: datetime, window_seconds: float
    ) -> deque[datetime]:
        calls = self._calls.setdefault(key, deque())
        cutoff = now.timestamp() - window_seconds
        while calls and calls[0].timestamp() <= cutoff:
            calls.popleft()
        return calls

    def evaluate(self, request: ToolPolicyRequest) -> ToolPolicyDecision:
        """Return the policy decision for one proposed call; denials are audited."""
        deny = ToolPolicyDecision(allowed=False, reason=ToolPolicyDecisionReason.CAPABILITY_OK)
        tool = self._tools.get_tool(request.tool_id)
        if tool is None:
            deny = ToolPolicyDecision(allowed=False, reason=ToolPolicyDecisionReason.TOOL_NOT_FOUND)
        elif not tool.enabled:
            deny = ToolPolicyDecision(allowed=False, reason=ToolPolicyDecisionReason.TOOL_DISABLED)
        else:
            sandbox = self._sandbox_for(request)
            violation = self._within_sandbox(tool.capability_scopes, tool.metadata, sandbox)
            if violation is not None:
                deny = ToolPolicyDecision(
                    allowed=False, reason=ToolPolicyDecisionReason.SANDBOX_VIOLATION
                )
            elif not self._tools.check_capability(
                request.agent_id, request.agent_version, request.tool_id
            ):
                deny = ToolPolicyDecision(
                    allowed=False, reason=ToolPolicyDecisionReason.CAPABILITY_DENIED
                )
            else:
                calls = self._prune(self._key(request), self._now(request), sandbox.window_seconds)
                if len(calls) >= sandbox.max_calls_per_window:
                    deny = ToolPolicyDecision(
                        allowed=False, reason=ToolPolicyDecisionReason.RATE_LIMITED
                    )
                else:
                    return ToolPolicyDecision(
                        allowed=True, reason=ToolPolicyDecisionReason.CAPABILITY_OK
                    )
        self._audit("tool_policy.denied", request, deny.reason.value)
        return deny

    @staticmethod
    def _key(request: ToolPolicyRequest) -> tuple[str, str, str]:
        return (request.agent_id, request.agent_version, request.tool_id)

    def record_call(
        self, request: ToolPolicyRequest, *, sandbox: SandboxPolicy | None = None
    ) -> ToolPolicyDecision:
        """Record one executed call; audit without argument content."""
        policy = sandbox or self._sandbox_for(request)
        now = self._now(request)
        calls = self._prune(self._key(request), now, policy.window_seconds)
        calls.append(now)
        self._audit(
            "tool_policy.call_recorded", request, ToolPolicyDecisionReason.CAPABILITY_OK.value
        )
        return ToolPolicyDecision(allowed=True, reason=ToolPolicyDecisionReason.CAPABILITY_OK)


def build_tool_policy(
    engine: object,
    sandbox_for: Callable[[ToolPolicyRequest], SandboxPolicy],
) -> ToolPolicyEngine:
    """Production wiring point composing the tool registry with policy gates."""
    from sqlalchemy.engine import Engine

    from clipos_api.tool_registry import ToolRegistryStore

    if not isinstance(engine, Engine):
        raise TypeError("engine must be a SQLAlchemy Engine")
    return ToolPolicyEngine(ToolRegistryStore.from_engine(engine), sandbox_for)
