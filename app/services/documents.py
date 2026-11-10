from datetime import date, timedelta

from app.core.enums import AccessLevel, DocStatus, HistoryDecision, Role
from app.core.errors import conflict, forbidden, not_found, unprocessable
from app.core.types import utcnow
from app.models import ApprovalHistory, Document, DocumentMetadata
from app.repositories.repos import CategoryRepo, DocumentRepo, FileRepo, MemberRepo, VersionRepo
from app.services import audit
from app.services.retention import retention_blocks_deletion
from app.services.categories import validate_metadata
from app.services.transitions import transition
import uuid

CREATE_ROLES = {Role.ORG_ADMIN, Role.DOCUMENT_MANAGER, Role.EMPLOYEE}


def build_search_text(doc, metadata: dict | None) -> str:
    parts = [doc.title, doc.description or "", doc.document_number, doc.category.name if doc.category else ""]
    parts += [str(v) for v in (metadata or {}).values()]
    return " ".join(p for p in parts if p)


class DocumentService:
    def __init__(self, db):
        self.db = db
        self.docs, self.cats, self.files, self.versions = (DocumentRepo(db), CategoryRepo(db), FileRepo(db),
                                                           VersionRepo(db))

    # ---------- access ----------
    def get_visible(self, ctx, document_id: int, lock: bool = False) -> Document:
        doc = (self.docs.lock_in_org if lock else self.docs.get_in_org)(ctx.org_id, document_id)
        if doc is None:
            raise not_found("DOCUMENT_NOT_FOUND", "Document not found.")
        if not self.docs.is_visible(ctx, document_id):
            raise forbidden("DOCUMENT_ACCESS_DENIED", "You are not permitted to access this document.")
        return doc

    @staticmethod
    def can_modify(ctx, doc) -> bool:
        if ctx.is_read_only or ctx.role == Role.REVIEWER:
            return False
        if ctx.is_manager:
            return True
        return doc.owner_id == ctx.user.id

    def require_modify(self, ctx, doc) -> None:
        if not self.can_modify(ctx, doc):
            raise forbidden("DOCUMENT_ACCESS_DENIED", "You are not permitted to modify this document.")

    def _validate_grants(self, ctx, user_ids: list[int]) -> list[int]:
        members = {m.user_id for m in MemberRepo(self.db).list(ctx.org_id) if m.is_active}
        bad = [u for u in user_ids if u not in members]
        if bad:
            raise unprocessable("INVALID_ACCESS_GRANT", "Users must be active members of the organization.",
                                {"user_ids": bad})
        return user_ids

    def metadata_of(self, doc) -> dict:
        row = self.docs.metadata_row(doc.id)
        return dict(row.data) if row and row.data else {}

    def reindex(self, doc) -> None:
        doc.search_text = build_search_text(doc, self.metadata_of(doc))

    def snapshot(self, doc) -> dict:
        return {"title": doc.title, "category_id": doc.category_id, "expiration_date":
                doc.expiration_date.isoformat() if doc.expiration_date else None, "metadata": self.metadata_of(doc)}

    def _sync_expiration(self, doc, metadata: dict) -> None:
        if metadata.get("expiration_date"):
            doc.expiration_date = date.fromisoformat(metadata["expiration_date"])

    # ---------- CRUD ----------
    def create(self, ctx, payload) -> Document:
        if not ctx.is_super and ctx.role not in CREATE_ROLES:
            raise forbidden("INSUFFICIENT_ROLE", "Your role cannot create documents.")
        cat = self.cats.get_in_org(ctx.org_id, payload.category_id)
        if cat is None or not cat.is_active:
            raise not_found("CATEGORY_NOT_FOUND", "Category not found or inactive.")
        data = validate_metadata(cat.definitions, payload.metadata)
        grants = self._validate_grants(ctx, payload.allowed_user_ids)
        doc = self.docs.add(Document(
            document_number=f"DOC-{utcnow().year}-{uuid.uuid4().hex[:10].upper()}", organization_id=ctx.org_id,
            title=payload.title.strip(), description=payload.description, category_id=cat.id, owner_id=ctx.user.id,
            access_level=payload.access_level.value, expiration_date=payload.expiration_date,
            status=DocStatus.DRAFT.value))
        doc.category = cat
        self._sync_expiration(doc, data)
        self.db.add(DocumentMetadata(document_id=doc.id, data=data, updated_by=ctx.user.id))
        self.docs.set_grants(doc.id, grants)
        doc.search_text = build_search_text(doc, data)
        audit.record(self.db, action="DOCUMENT_CREATED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta={"title": doc.title, "category_id": cat.id, "document_number": doc.document_number})
        self.db.commit()
        return doc

    def detail(self, ctx, document_id: int):
        doc = self.get_visible(ctx, document_id)
        return doc, self.metadata_of(doc)

    def update(self, ctx, document_id: int, payload) -> Document:
        doc = self.get_visible(ctx, document_id, lock=True)
        self.require_modify(ctx, doc)
        self._require_editable(doc)
        changes = payload.model_dump(exclude_unset=True)
        before = {k: str(getattr(doc, k)) for k in changes if k != "allowed_user_ids"}
        if "allowed_user_ids" in changes and changes["allowed_user_ids"] is not None:
            self.docs.set_grants(doc.id, self._validate_grants(ctx, changes.pop("allowed_user_ids")))
        else:
            changes.pop("allowed_user_ids", None)
        for k, v in changes.items():
            if k in ("title", "access_level") and v is None:
                continue
            setattr(doc, k, v.value if hasattr(v, "value") else v)
        if doc.status == DocStatus.REJECTED:
            transition(doc, DocStatus.DRAFT)
        self.reindex(doc)
        audit.record(self.db, action="DOCUMENT_UPDATED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta={"before": before, "after": {k: str(v) for k, v in changes.items()}})
        self.db.commit()
        return doc

    @staticmethod
    def _require_editable(doc) -> None:
        if doc.status not in (DocStatus.DRAFT, DocStatus.REJECTED):
            raise conflict("DOCUMENT_NOT_EDITABLE",
                           f"A document in status {doc.status} cannot be edited; upload a new version instead.")

    def update_metadata(self, ctx, document_id: int, values: dict) -> dict:
        doc = self.get_visible(ctx, document_id, lock=True)
        self.require_modify(ctx, doc)
        self._require_editable(doc)
        cat = self.cats.get(doc.category_id)
        before = self.metadata_of(doc)
        merged = {**before, **{k: v for k, v in values.items()}}
        merged = {k: v for k, v in merged.items() if v not in (None, "")}
        clean = validate_metadata(cat.definitions, merged)
        row = self.docs.metadata_row(doc.id)
        if row is None:
            row = DocumentMetadata(document_id=doc.id)
            self.db.add(row)
        row.data, row.updated_by = clean, ctx.user.id
        self._sync_expiration(doc, clean)
        if doc.status == DocStatus.REJECTED:
            transition(doc, DocStatus.DRAFT)
        self.reindex(doc)
        audit.record(self.db, action="METADATA_UPDATED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"before": before, "after": clean})
        self.db.commit()
        return clean

    def metadata(self, ctx, document_id: int):
        doc = self.get_visible(ctx, document_id)
        row = self.docs.metadata_row(doc.id)
        return {"data": self.metadata_of(doc), "suggested": row.suggested if row else None}

    def delete(self, ctx, document_id: int) -> None:
        doc = self.get_visible(ctx, document_id, lock=True)
        if ctx.is_read_only or ctx.role == Role.REVIEWER or not (ctx.is_manager or doc.owner_id == ctx.user.id):
            raise forbidden("DOCUMENT_ACCESS_DENIED", "You are not permitted to delete this document.")
        if doc.status in (DocStatus.SUBMITTED, DocStatus.UNDER_REVIEW, DocStatus.APPROVED):
            raise conflict("DOCUMENT_DELETE_NOT_ALLOWED",
                           f"Documents in status {doc.status} cannot be deleted; archive them instead.")
        if retention_blocks_deletion(self.db, doc):
            raise conflict("DOCUMENT_RETENTION_ACTIVE", "A retention policy prohibits deleting this document.")
        doc.deleted_at = utcnow()  # soft delete: versions, files, reviews and audit rows are preserved
        audit.record(self.db, action="DOCUMENT_DELETED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"title": doc.title})
        self.db.commit()

    # ---------- lifecycle ----------
    def archive(self, ctx, document_id: int) -> Document:
        doc = self.get_visible(ctx, document_id, lock=True)
        self.require_modify(ctx, doc)
        previous = doc.status
        transition(doc, DocStatus.ARCHIVED)
        doc.previous_status = previous
        audit.record(self.db, action="DOCUMENT_ARCHIVED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"from": previous})
        self.db.commit()
        return doc

    def restore(self, ctx, document_id: int) -> Document:
        doc = self.get_visible(ctx, document_id, lock=True)
        self.require_modify(ctx, doc)
        if doc.status != DocStatus.ARCHIVED:
            raise conflict("INVALID_WORKFLOW_TRANSITION", "Only archived documents can be restored.")
        target = doc.previous_status or DocStatus.DRAFT.value
        transition(doc, target)
        doc.previous_status = None
        audit.record(self.db, action="DOCUMENT_RESTORED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"to": target})
        self.db.commit()
        return doc

    def renew(self, ctx, document_id: int, new_date: date, comment: str | None) -> Document:
        doc = self.get_visible(ctx, document_id, lock=True)
        if not ctx.is_manager:
            raise forbidden("INSUFFICIENT_ROLE", "Only managers and admins can renew documents.")
        if doc.status not in (DocStatus.EXPIRED, DocStatus.APPROVED):
            raise conflict("INVALID_WORKFLOW_TRANSITION", "Only approved or expired documents can be renewed.")
        if new_date <= date.today():
            raise unprocessable("INVALID_EXPIRATION_DATE", "The new expiration date must be in the future.")
        old = doc.expiration_date
        if doc.status == DocStatus.EXPIRED:
            transition(doc, DocStatus.APPROVED)
        doc.expiration_date = new_date
        row = self.docs.metadata_row(doc.id)
        if row and "expiration_date" in (row.data or {}):
            row.data = {**row.data, "expiration_date": new_date.isoformat()}
        self.db.add(ApprovalHistory(document_id=doc.id, version_number=doc.current_version,
                                    submission_no=doc.submission_no, actor_id=ctx.user.id,
                                    decision=HistoryDecision.RENEWED.value, comment=comment,
                                    metadata_snapshot=self.snapshot(doc)))
        audit.record(self.db, action="DOCUMENT_RENEWED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta={"old_expiration": old, "new_expiration": new_date})
        self.db.commit()
        return doc

    # ---------- queries ----------
    def search(self, ctx, **filters):
        return self.docs.search(ctx, **filters)

    def expiring(self, ctx, days: int, page: int, page_size: int):
        today = date.today()
        return self.docs.search(ctx, status=DocStatus.APPROVED.value, expires_after=today,
                                expires_before=today + timedelta(days=days), sort_by="expiration_date",
                                sort_order="asc", page=page, page_size=page_size)

    def expired(self, ctx, page: int, page_size: int):
        return self.docs.search(ctx, status=DocStatus.EXPIRED.value, sort_by="expiration_date",
                                sort_order="desc", page=page, page_size=page_size)

    def current_file(self, ctx, document_id: int):
        doc = self.get_visible(ctx, document_id)
        version = self.versions.by_number(doc.id, doc.current_version) if doc.current_version else None
        if version is None:
            raise not_found("FILE_NOT_FOUND", "This document has no uploaded version yet.")
        return doc, version.file

    def history(self, ctx, document_id: int):
        from app.repositories.repos import ReviewRepo
        doc = self.get_visible(ctx, document_id)
        return ReviewRepo(self.db).history(doc.id)
