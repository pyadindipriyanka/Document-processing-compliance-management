from datetime import date

from app.core.enums import FieldType
from app.core.errors import AppError, conflict, not_found, unprocessable
from app.models import DocumentCategory, MetadataDefinition
from app.repositories.repos import CategoryRepo
from app.services import audit


def _coerce(defn, value):
    t = defn.field_type
    if t == FieldType.STRING:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value.strip()
    if t == FieldType.DATE:
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise ValueError("must be an ISO date (YYYY-MM-DD)")
    if t == FieldType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        return value
    if t == FieldType.BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError("must be a boolean")
        return value
    raise ValueError("unsupported field type")


def validate_metadata(definitions, data: dict, enforce_required: bool = True) -> dict:
    """Validates `data` against the category configuration and returns the normalised dict."""
    by_key = {d.key: d for d in definitions}
    unknown = [k for k in data if k not in by_key]
    if unknown:
        raise unprocessable("INVALID_METADATA", "Unknown metadata fields for this category.", {"unknown": unknown})
    clean, problems = {}, {}
    for key, value in data.items():
        if value is None or value == "":
            continue
        try:
            clean[key] = _coerce(by_key[key], value)
        except ValueError as exc:
            problems[key] = str(exc)
    if problems:
        raise unprocessable("INVALID_METADATA", "Some metadata values are invalid.", problems)
    if enforce_required:
        missing = [d.key for d in definitions if d.required and d.key not in clean]
        if missing:
            raise unprocessable("REQUIRED_METADATA_MISSING", "Required metadata fields are missing.",
                                {"missing": missing})
    return clean


class CategoryService:
    def __init__(self, db):
        self.db, self.repo = db, CategoryRepo(db)

    def _defs(self, items):
        keys = [i.key for i in items]
        if len(keys) != len(set(keys)):
            raise unprocessable("INVALID_METADATA_DEFINITION", "Duplicate metadata keys.")
        return [MetadataDefinition(key=i.key, label=i.label, field_type=str(i.field_type.value), required=i.required)
                for i in items]

    def create(self, ctx, payload):
        if self.repo.by_name(ctx.org_id, payload.name):
            raise conflict("CATEGORY_EXISTS", "A category with this name already exists.")
        cat = DocumentCategory(organization_id=ctx.org_id, name=payload.name.strip(), description=payload.description)
        cat.definitions = self._defs(payload.metadata_definitions)
        self.repo.add(cat)
        audit.record(self.db, action="CATEGORY_CREATED", entity_type="category", entity_id=cat.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"name": cat.name})
        self.db.commit()
        return cat

    def get(self, ctx, category_id: int):
        cat = self.repo.get_in_org(ctx.org_id, category_id)
        if cat is None:
            raise not_found("CATEGORY_NOT_FOUND", "Category not found.")
        return cat

    def update(self, ctx, category_id: int, payload):
        cat = self.get(ctx, category_id)
        if payload.name and payload.name.lower() != cat.name.lower() and self.repo.by_name(ctx.org_id, payload.name):
            raise conflict("CATEGORY_EXISTS", "A category with this name already exists.")
        if payload.name is not None:
            cat.name = payload.name.strip()
        if payload.description is not None:
            cat.description = payload.description
        if payload.is_active is not None:
            cat.is_active = payload.is_active
        if payload.metadata_definitions is not None:
            cat.definitions.clear()
            self.db.flush()
            cat.definitions.extend(self._defs(payload.metadata_definitions))
        audit.record(self.db, action="CATEGORY_UPDATED", entity_type="category", entity_id=cat.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta=payload.model_dump(mode="json", exclude_none=True))
        self.db.commit()
        return cat

    def delete(self, ctx, category_id: int):
        cat = self.get(ctx, category_id)
        if self.repo.document_count(cat.id):
            raise conflict("CATEGORY_IN_USE", "Category has documents; deactivate it instead.")
        audit.record(self.db, action="CATEGORY_DELETED", entity_type="category", entity_id=cat.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"name": cat.name})
        self.db.delete(cat)
        self.db.commit()
