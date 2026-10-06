"""Explicit bounded module boundaries."""

from enum import StrEnum


class BoundedModule(StrEnum):
    IDENTITY = "identity"
    ORGANIZATIONS = "organizations"
    GOVERNANCE = "governance"
    BRANDS = "brands"
    SIGNALS = "signals"
    RIGHTS = "rights"
    SOURCES = "sources"
    RESEARCH = "research"
    CONTENT_PLANNING = "content_planning"
    MEDIA = "media"
    PUBLISHING = "publishing"
    ANALYTICS = "analytics"
    MONETIZATION = "monetization"
    FINANCE = "finance"
    EXPERIMENTS = "experiments"
    AGENTS = "agents"
    MODELS = "models"
    TOOLS_PLUGINS = "tools_plugins"
    WORKFLOWS = "workflows"
    OPERATIONS = "operations"
    NOTIFICATIONS = "notifications"


EXPECTED_MODULES = frozenset(BoundedModule)
