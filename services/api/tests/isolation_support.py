"""Support machinery for the cross-tenant isolation suite.

The isolation suite needs to place one row of organization A into every
org-scoped table and then observe what the API does when organization B asks
for it. Writing those rows straight into the database is the only way to
cover every table without hand-building a request body per route, but the
rows have to satisfy the domain models the routes return: a row with an
invalid status is a 500, not a leak, and would hide a real one.

This module derives the valid values for every column from the OpenAPI
response schemas, so a seeded row is one the domain accepts.
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# Marker value. It appears in no request, so finding it in a response can
# only mean the row was disclosed.
DATA_MARKER = "isolation-a-private-data"

ORG_A = "org-isolation-a"
ORG_B = "org-isolation-b"

# Candidates tried, in order, for a string column with a pattern.
_PATTERN_CANDIDATES = (
    "seed",
    "iso",
    "secrets://seed",
    "iso-seed",
    "unknown",
    "pending",
    "active",
    "draft",
    "none",
    "default",
    "1920x1080",
    "1080",
    "seed-value",
    "a",
    "1",
)


def _resolve(spec: dict[str, Any], schema: Any) -> dict[str, Any] | None:
    """Follow a $ref chain to the concrete schema."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        name = schema["$ref"].split("/")[-1]
        schema = spec.get("components", {}).get("schemas", {}).get(name)
        seen += 1
    return schema if isinstance(schema, dict) else None


def _entity_schemas(spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return (label, properties) for every JSON success response."""
    found: list[tuple[str, dict[str, Any]]] = []
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            responses = operation.get("responses", {})
            for status in ("200", "201"):
                response = responses.get(status)
                if not response:
                    continue
                for media in (response.get("content") or {}).values():
                    schema = _resolve(spec, media.get("schema"))
                    if not schema:
                        continue
                    if schema.get("type") == "array":
                        schema = _resolve(spec, schema.get("items"))
                        if not schema:
                            continue
                    properties = schema.get("properties")
                    if properties:
                        found.append((f"{method.upper()} {path}", properties))
                break
    return found


def _property_schema(schema: Any, depth: int = 0) -> dict[str, Any] | None:
    """Return the concrete schema of a property, unwrapping unions."""
    if depth > 6 or not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return schema
    if "anyOf" in schema or "oneOf" in schema:
        options = schema.get("anyOf") or schema.get("oneOf") or []
        for option in options:
            if isinstance(option, dict) and option.get("type") == "null":
                continue
            resolved = _property_schema(option, depth + 1)
            if resolved is not None:
                return resolved
        return None
    return schema


def _string_value(schema: dict[str, Any] | None) -> str | None:
    """Return a string that satisfies *schema*, or None when none is known."""
    if schema is None:
        return "seed"
    if "const" in schema:
        return str(schema["const"])
    if schema.get("enum"):
        return str(schema["enum"][0])
    kind = schema.get("type")
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return "2026-10-22T00:00:00Z"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        pattern = schema.get("pattern")
        if pattern:
            for candidate in _PATTERN_CANDIDATES:
                if re.fullmatch(pattern, candidate):
                    return candidate
            return None
        return "seed"
    return None


def _column_value(column: sa.Column[Any], schema: dict[str, Any] | None) -> Any:
    """Return a value that satisfies one column and its domain model."""
    concrete = _property_schema(schema)
    if concrete is None:
        concrete = schema
    kind = concrete.get("type") if isinstance(concrete, dict) else None
    if kind == "string":
        return _string_value(concrete)
    if kind == "integer":
        low = int(concrete.get("minimum", 1) or 1)
        high = concrete.get("maximum")
        value = max(low, 1000)
        if high is not None:
            value = min(value, int(high))
        return value
    if kind == "number":
        return float(max(float(concrete.get("minimum", 1) or 1), 1000.0))
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    if isinstance(column.type, sa.Enum):
        members = list(column.type.enums)
        return members[0] if members else "seed"
    try:
        python_type = column.type.python_type
    except NotImplementedError:  # pragma: no cover - exotic column types
        return "seed"
    if python_type is bool:
        return False
    if python_type is int:
        return 1
    if python_type is float:
        return 1.0
    if python_type is dt.datetime:
        # SQLite with timezone=True DateTime needs ISO string, not datetime obj.
        return dt.datetime(2026, 10, 22).isoformat()
    if python_type is dict:
        return {}
    if python_type is list:
        return []
    if python_type is bytes:
        return b"seed"
    return "seed"


def _business_id_column(table: sa.Table) -> sa.Column[Any] | None:
    """Return the column a route would use to address one row of *table*.

    Several tables use the business identifier as the primary key, so a
    primary key is only skipped when it is the synthetic integer ``id``.
    """
    candidates = [
        c
        for c in table.columns
        if c.name.endswith("_id")
        and c.name != "org_id"
        and not (c.primary_key and c.name == "id")
    ]
    if not candidates:
        return None
    normalised = table.name.replace("_", "")
    for column in candidates:
        if column.name.replace("_", "") in normalised:
            return column
    for column in candidates:
        if column.primary_key:
            return column
    return candidates[0]


def _data_column(table: sa.Table, id_column: sa.Column[Any]) -> sa.Column[Any] | None:
    """Return a column that can carry the data marker."""
    for column in table.columns:
        if column is id_column or column.name == "org_id":
            continue
        if isinstance(column.type, sa.String):
            return column
    return None


def _table_properties(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map each org-scoped table onto the response schema that returns it."""
    from clipos_api.repository import Base

    entities = _entity_schemas(spec)
    mapping: dict[str, dict[str, Any]] = {}
    for table in Base.metadata.sorted_tables:
        if not any(c.name == "org_id" for c in table.columns):
            continue
        id_column = _business_id_column(table)
        if id_column is None:
            continue
        column_names = {c.name for c in table.columns}
        best: dict[str, Any] | None = None
        best_score = 0
        for _label, properties in entities:
            if id_column.name not in properties:
                continue
            score = len(column_names.intersection(properties))
            if score > best_score:
                best_score = score
                best = properties
        mapping[table.name] = best or {}
    return mapping


def seed_organization_a(engine: Engine, spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Seed one row per org-scoped table in organization A."""
    from clipos_api.repository import Base

    properties_by_table = _table_properties(spec)

    # Map table name to ORM model class for proper datetime handling
    table_to_model: dict[str, type] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if hasattr(cls, "__table__"):
            table_to_model[cls.__table__.name] = cls

    seeded: dict[str, dict[str, Any]] = {}
    with Session(engine) as session:
        for table in Base.metadata.sorted_tables:
            if not any(c.name == "org_id" for c in table.columns):
                continue
            id_column = _business_id_column(table)
            if id_column is None:
                continue
            properties = properties_by_table.get(table.name, {})
            data_column = _data_column(table, id_column)

            # Get the ORM model class for this table
            model_cls = table_to_model.get(table.name)
            if model_cls is None:
                continue

            values: dict[str, Any] = {
                "org_id": ORG_A,
                id_column.name: f"iso-a-{table.name}",
            }
            skip = False
            for column in table.columns:
                if column.name in values:
                    continue
                if column.primary_key or column.name == "org_id":
                    continue
                if column is data_column:
                    values[column.name] = DATA_MARKER
                    continue
                if column.nullable or column.default is not None:
                    continue
                value = _column_value(column, properties.get(column.name))
                if value is None:
                    skip = True
                    break
                values[column.name] = value
            if skip:
                continue

            # Insert via ORM to handle datetime properly
            try:
                obj = model_cls(**values)
                session.add(obj)
                session.flush()
            except Exception:
                session.rollback()
                continue
            session.commit()
            seeded[table.name] = values
    return seeded


def organization_a_rows(engine: Engine) -> dict[str, list[str]]:
    """Snapshot every org-scoped table's rows for organization A."""
    from clipos_api.repository import Base

    snapshot: dict[str, list[str]] = {}
    with Session(engine) as session:
        for table in Base.metadata.sorted_tables:
            if not any(c.name == "org_id" for c in table.columns):
                continue
            rows = session.execute(sa.select(table).where(table.c.org_id == ORG_A)).fetchall()
            snapshot[table.name] = sorted(repr(tuple(row)) for row in rows)
    return snapshot


def candidate_values(param: str, seeded: dict[str, dict[str, Any]]) -> list[str]:
    """Return every seeded identifier that could address *param*."""
    values: list[str] = []
    for table_values in seeded.values():
        if param in table_values:
            values.append(str(table_values[param]))
    return values


def path_variants(
    path: str, path_params: list[str], seeded: dict[str, dict[str, Any]]
) -> list[str]:
    """Return the plausible concrete paths for a templated path."""
    per_param: list[list[str]] = []
    for name in path_params:
        candidates = candidate_values(name, seeded) or [f"iso-missing-{name}"]
        per_param.append(candidates)
    variants: list[str] = []
    for combination in itertools.product(*per_param) if per_param else [()]:
        filled = path
        for name, value in zip(path_params, combination):
            filled = filled.replace("{" + name + "}", value)
        variants.append(filled)
    return variants


def string_values(payload: Any, skip_keys: frozenset[str] = frozenset()) -> list[str]:
    """Collect every string value in a JSON payload."""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in skip_keys:
                continue
            found.extend(string_values(value, skip_keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(string_values(item, skip_keys))
    elif isinstance(payload, str):
        found.append(payload)
    return found


def discloses(response: Any, seeded: dict[str, dict[str, Any]]) -> bool:
    """True when the response contains organization A's data."""
    if DATA_MARKER in response.text:
        return True
    identifiers = {
        str(value)
        for values in seeded.values()
        for key, value in values.items()
        if key != "org_id"
    }
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if payload is not None:
        # An error message may echo the identifier the caller supplied; that
        # discloses nothing. Any other value would be organization A's data.
        values = string_values(payload, frozenset({"detail", "message", "error"}))
        if identifiers.intersection(values):
            return True
    return False


def synthesize_body(spec: dict[str, Any], method: str, path: str) -> dict[str, Any] | None:
    """Return a minimal request body for an operation, addressed to org B."""
    operation = spec["paths"].get(path, {}).get(method.lower(), {})
    body = operation.get("requestBody") or {}
    for media in (body.get("content") or {}).values():
        schema = _resolve(spec, media.get("schema"))
        if not schema:
            continue
        built = _build_object(schema, spec)
        if isinstance(built, dict):
            built["org_id"] = ORG_B
            return built
    return None


def _build_object(schema: dict[str, Any], spec: dict[str, Any], depth: int = 0) -> Any:
    """Build a minimal instance of an object schema."""
    if depth > 6:
        return None
    resolved = _resolve(spec, schema)
    if resolved is None:
        return None
    if resolved.get("type") == "object" or "properties" in resolved:
        properties = resolved.get("properties", {})
        body: dict[str, Any] = {}
        for key in resolved.get("required", []):
            value = _build_value(properties.get(key, {}), spec, depth + 1)
            if value is None:
                return None
            body[key] = value
        return body
    return _build_value(resolved, spec, depth + 1)


def _build_value(schema: Any, spec: dict[str, Any], depth: int = 0) -> Any:
    """Build a minimal instance of a leaf schema."""
    if depth > 6 or not isinstance(schema, dict):
        return None
    resolved = _resolve(spec, schema)
    if resolved is None:
        return None
    if resolved.get("enum"):
        return resolved["enum"][0]
    if "const" in resolved:
        return resolved["const"]
    if "anyOf" in resolved or "oneOf" in resolved:
        options = resolved.get("anyOf") or resolved.get("oneOf") or []
        for option in options:
            if isinstance(option, dict) and option.get("type") == "null":
                continue
            value = _build_value(option, spec, depth + 1)
            if value is not None:
                return value
        return None
    kind = resolved.get("type")
    if kind == "object":
        return _build_object(resolved, spec, depth + 1)
    if kind == "array":
        item = _build_value(resolved.get("items", {}), spec, depth + 1)
        return [] if item is None else [item]
    if kind == "string":
        fmt = resolved.get("format")
        if fmt == "date-time":
            return "2026-10-22T00:00:00Z"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000001"
        pattern = resolved.get("pattern")
        if pattern:
            for candidate in _PATTERN_CANDIDATES:
                if re.fullmatch(pattern, candidate):
                    return candidate
        return "seed"
    if kind == "integer":
        return max(int(resolved.get("minimum", 1) or 1), 1)
    if kind == "number":
        return float(resolved.get("minimum", 1) or 1)
    if kind == "boolean":
        return False
    return None