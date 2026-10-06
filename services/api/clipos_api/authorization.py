"""RBAC/capability authorization: built-in roles, deny-by-default service, capability policy.

CLIP-008 ships frozen BUILT-IN role sets only. Custom role editing and
``role_permissions`` persistence are deferred to the SQL era (CLIP-010+), and
service identity *storage* is CLIP-009. Built-in role/persona mapping:

=================== =========================================================
 Built-in role        Mapped persona / intent
=================== =========================================================
``owner``           Owner / Super Admin (01 §3): all permissions, including
                    irreversible ``org:delete``/``org:transfer`` and
                    ``killswitch:activate``.
``admin``           Operator (01 §3): control-plane administration within the
                    org; explicitly **not** org transfer/delete and **not**
                    the global kill switch.
``member``          CLIP-007 membership ``member``: read-oriented access,
                    in-org settings updates, and content review; no
                    destructive or membership administration.
``reviewer``        Reviewer (01 §3): audit visibility and review/publish
                    decisions only; no organization or budget administration.
``service_agent``   Service Agent (01 §3): least-privilege machine persona;
                    starts with ``content:review`` only and is never an
                    organization administrator.
=================== =========================================================

Authorization is deny by default. Cross-tenant safety relies on
``TenancyService.require_membership`` (the CLIP-007 primitive): only the ACTIVE
membership in the requested org is considered, so membership in another
organization can never satisfy an authorization check. Denied WRITE-class
checks (any action other than ``read``) are audited as ``authz.denied``; allows
are never audited to avoid noise.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clipos_api import audit

_PERMISSION_PATTERN = r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$"


class Permission(BaseModel):
    """Validated ``resource:action`` permission string."""

    model_config = ConfigDict(frozen=True)

    value: str = Field(pattern=_PERMISSION_PATTERN)

    @classmethod
    def model_validate(cls, obj: Any, *args: object, **kwargs: object) -> Self:
        if isinstance(obj, str):
            return cls(value=obj)
        return super().model_validate(obj, *args, **kwargs)  # type: ignore[arg-type]

    @field_validator("value", mode="before")
    @classmethod
    def _coerce(cls, value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("permission must be a string")
        return value


ORG_READ = Permission(value="org:read")
ORG_UPDATE = Permission(value="org:update")
ORG_DELETE = Permission(value="org:delete")
ORG_TRANSFER = Permission(value="org:transfer")
MEMBER_INVITE = Permission(value="member:invite")
MEMBER_REMOVE = Permission(value="member:remove")
MEMBER_ROLE_CHANGE = Permission(value="member:role_change")
AUDIT_READ = Permission(value="audit:read")
SETTINGS_UPDATE = Permission(value="settings:update")
CONTENT_REVIEW = Permission(value="content:review")
CONTENT_PUBLISH = Permission(value="content:publish")
BUDGET_UPDATE = Permission(value="budget:update")
KILLSWITCH_ACTIVATE = Permission(value="killswitch:activate")

OWNER_PERMISSIONS = frozenset(
    {
        ORG_READ,
        ORG_UPDATE,
        ORG_DELETE,
        ORG_TRANSFER,
        MEMBER_INVITE,
        MEMBER_REMOVE,
        MEMBER_ROLE_CHANGE,
        AUDIT_READ,
        SETTINGS_UPDATE,
        CONTENT_REVIEW,
        CONTENT_PUBLISH,
        BUDGET_UPDATE,
        KILLSWITCH_ACTIVATE,
    }
)
ADMIN_PERMISSIONS = frozenset(
    {
        ORG_READ,
        ORG_UPDATE,
        MEMBER_INVITE,
        MEMBER_REMOVE,
        MEMBER_ROLE_CHANGE,
        AUDIT_READ,
        SETTINGS_UPDATE,
        CONTENT_REVIEW,
        CONTENT_PUBLISH,
        BUDGET_UPDATE,
    }
)
MEMBER_PERMISSIONS = frozenset(
    {
        ORG_READ,
        AUDIT_READ,
        SETTINGS_UPDATE,
        CONTENT_REVIEW,
    }
)
REVIEWER_PERMISSIONS = frozenset(
    {
        AUDIT_READ,
        CONTENT_REVIEW,
        CONTENT_PUBLISH,
    }
)
SERVICE_AGENT_PERMISSIONS = frozenset({CONTENT_REVIEW})


class RoleDefinition(BaseModel):
    """A built-in role's frozen permission set."""

    model_config = ConfigDict(frozen=True)

    name: str
    permissions: frozenset[Permission]
    description: str


def _role(name: str, permissions: frozenset[Permission], description: str) -> RoleDefinition:
    return RoleDefinition(name=name, permissions=permissions, description=description)


BUILTIN_ROLES: dict[str, RoleDefinition] = {
    "owner": _role("owner", OWNER_PERMISSIONS, "Owner/Super Admin: all permissions"),
    "admin": _role(
        "admin",
        ADMIN_PERMISSIONS,
        "Operator: org administration, excluding org transfer/delete and kill switch",
    ),
    "member": _role(
        "member",
        MEMBER_PERMISSIONS,
        "Member: read-oriented access, settings updates, and content review",
    ),
    "reviewer": _role(
        "reviewer",
        REVIEWER_PERMISSIONS,
        "Reviewer: audit visibility and review/publish decisions only",
    ),
    "service_agent": _role(
        "service_agent",
        SERVICE_AGENT_PERMISSIONS,
        "Service Agent: least-privilege machine persona",
    ),
}


class Decision(BaseModel):
    """A single authorization result."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    permission: str
    reason: str
    role_used: str | None = None


class AuthorizationService:
    """Tenant-scoped, deny-by-default RBAC checks over built-in roles."""

    def __init__(self, tenancy_service: Any) -> None:
        self._tenancy = tenancy_service

    def authorize(
        self,
        user_id: str,
        org_id: str,
        permission: Permission,
        *,
        actor_context: Any | None = None,
    ) -> Decision:
        """Authorize against the caller's active membership in ``org_id`` only."""
        del actor_context
        try:
            requested = Permission(value=permission.value)
        except ValueError as exc:
            return Decision(
                allowed=False, permission=str(permission), reason=f"invalid_permission: {exc}"
            )

        membership = self._tenancy.require_membership(user_id, org_id)
        if membership is None:
            return self._deny(user_id, org_id, requested, "no_active_membership", None)

        role_name = str(membership.role)
        role = BUILTIN_ROLES.get(role_name)
        if role is None:
            return self._deny(user_id, org_id, requested, "unknown_role", role_name)
        if requested not in role.permissions:
            return self._deny(user_id, org_id, requested, "permission_not_granted", role_name)
        return Decision(
            allowed=True, permission=requested.value, reason="granted", role_used=role_name
        )

    def _deny(
        self,
        user_id: str,
        org_id: str,
        permission: Permission,
        reason: str,
        role_used: str | None,
    ) -> Decision:
        decision = Decision(
            allowed=False,
            permission=permission.value,
            reason=reason,
            role_used=role_used,
        )
        if _is_write_class(permission.value):
            audit.record(
                "authz.denied",
                user_id,
                org_id,
                {
                    "permission": permission.value,
                    "reason": reason,
                    "role_used": role_used,
                },
            )
        return decision


def _is_write_class(permission_value: str) -> bool:
    return permission_value.split(":", 1)[1] != "read"


GPU = "gpu"
NETWORK_EGRESS = "network_egress"
MEDIA_WRITE = "media_write"
SECRETS_READ = "secrets_read"


class CapabilityPolicy:
    """Tool-call capability allowlist; GPU and everything else are never assumed."""

    def __init__(self, allowed: frozenset[str] = frozenset()) -> None:
        self._allowed = frozenset(allowed)

    def permit(self, tool_name: str, required: Iterable[str]) -> bool:
        if not tool_name:
            raise ValueError("tool_name is required")
        return frozenset(required).issubset(self._allowed)

    def audit_policy_denial(self, tool_name: str, required: Iterable[str]) -> dict[str, Any]:
        """Record a ``capability.denied`` event for an observed tool denial."""
        if not tool_name:
            raise ValueError("tool_name is required")
        return audit.record(
            "capability.denied",
            "capability-policy",
            tool_name,
            {"required": sorted(set(required))},
        )
