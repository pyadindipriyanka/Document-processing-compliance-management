from app.core.context import correlation_id_var
from app.core.types import utcnow
from app.models import AuditLog

_SENSITIVE = ("password", "token", "secret", "authorization", "api_key", "credential")


def _redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(s in str(k).lower() for s in _SENSITIVE) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def record(db, *, action: str, entity_type: str, entity_id=None, actor_id=None, org_id=None, meta=None) -> None:
    """Adds an audit row to the caller's transaction, so it commits (or rolls back) with the operation."""
    db.add(AuditLog(actor_id=actor_id, organization_id=org_id, action=action, entity_type=entity_type,
                    entity_id=None if entity_id is None else str(entity_id),
                    correlation_id=correlation_id_var.get(), details=_redact(meta or {}), created_at=utcnow()))
