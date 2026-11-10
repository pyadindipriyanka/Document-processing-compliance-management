from sqlalchemy.exc import IntegrityError

from app.core.enums import DocStatus, HistoryDecision, JobType, NotificationType
from app.core.errors import conflict, not_found
from app.models import ApprovalHistory, DocumentVersion
from app.repositories.repos import ReviewRepo, VersionRepo
from app.services import audit
from app.services.documents import DocumentService
from app.services.files import FileService
from app.services.jobs import enqueue
from app.services.notifications import notify
from app.services.transitions import transition
from app.core.enums import ReviewStatus
from app.core.types import utcnow


class VersionService:
    def __init__(self, db, storage=None):
        self.db = db
        self.versions, self.reviews = VersionRepo(db), ReviewRepo(db)
        self.documents, self.files = DocumentService(db), FileService(db, storage)

    # ---------- reads ----------
    def list(self, ctx, document_id: int):
        doc = self.documents.get_visible(ctx, document_id)
        return self.versions.list(doc.id)

    def get(self, ctx, document_id: int, version_id: int):
        doc = self.documents.get_visible(ctx, document_id)
        v = self.versions.get_in_document(doc.id, version_id)
        if v is None:
            raise not_found("INVALID_DOCUMENT_VERSION", "Version not found for this document.")
        return v

    # ---------- approval invalidation ----------
    def _invalidate_approval(self, ctx, doc, reason: str) -> None:
        """A material change makes any earlier approval void. History rows stay, tied to the old version."""
        if doc.status == DocStatus.DRAFT:
            return
        if doc.status in (DocStatus.APPROVED, DocStatus.UNDER_REVIEW, DocStatus.SUBMITTED) and doc.current_version:
            self.db.add(ApprovalHistory(document_id=doc.id, version_number=doc.current_version,
                                        submission_no=doc.submission_no, stage_order=doc.current_stage,
                                        actor_id=ctx.user.id, decision=HistoryDecision.INVALIDATED.value,
                                        comment=reason, metadata_snapshot=self.documents.snapshot(doc)))
        for r in self.reviews.pending_for_document(doc.id):
            r.status, r.decided_at = ReviewStatus.CANCELLED.value, utcnow()
        transition(doc, DocStatus.DRAFT)
        doc.current_stage = None

    # ---------- writes ----------
    def create_version(self, ctx, document_id: int, upload, change_description: str | None, is_material: bool = True,
                       *, restored_from=None, file_row=None):
        doc = self.documents.get_visible(ctx, document_id, lock=True)  # row lock serialises version numbering
        self.documents.require_modify(ctx, doc)
        if doc.status == DocStatus.ARCHIVED:
            raise conflict("DOCUMENT_ARCHIVED", "Archived documents must be restored before changing them.")
        if doc.status == DocStatus.SUBMITTED:
            raise conflict("DOCUMENT_NOT_EDITABLE", "The document is being submitted; try again shortly.")
        new_file = None
        try:
            if restored_from is None:
                new_file = self.files.store_for_document(ctx, doc, upload)
                file_row = new_file
            number = max(doc.current_version, self.versions.max_number(doc.id)) + 1
            was_review_active = doc.status == DocStatus.UNDER_REVIEW
            reviewers = [r.reviewer_id for r in self.reviews.pending_for_document(doc.id)]
            if is_material:
                self._invalidate_approval(ctx, doc, f"Material change: version {number} uploaded")
            version = DocumentVersion(
                document_id=doc.id, version_number=number, file_id=file_row.id, uploaded_by=ctx.user.id,
                change_description=change_description, checksum_sha256=file_row.checksum_sha256,
                is_material=is_material,
                restored_from_version_id=restored_from.id if restored_from else None)
            self.db.add(version)
            self.db.flush()
            doc.current_version = number
            audit.record(self.db, action="VERSION_RESTORED" if restored_from else "VERSION_CREATED",
                         entity_type="document_version", entity_id=version.id, actor_id=ctx.user.id,
                         org_id=ctx.org_id, meta={"document_id": doc.id, "version_number": number,
                                                  "sha256": version.checksum_sha256, "material": is_material,
                                                  "restored_from": restored_from.version_number if restored_from else None})
            recipients = {doc.owner_id, *(reviewers if not is_material and was_review_active else [])}
            for uid in recipients - {ctx.user.id}:
                notify(self.db, user_id=uid, org_id=ctx.org_id, type_=NotificationType.VERSION_UPLOADED,
                       title=f"New version of {doc.title}", message=f"Version {number} was uploaded.",
                       entity_type="document", entity_id=doc.id, dedupe_key=f"version:{version.id}:{uid}")
            enqueue(self.db, JobType.CHECKSUM_VERIFY, f"checksum:{version.id}", {"file_id": file_row.id})
            enqueue(self.db, JobType.METADATA_EXTRACTION, f"extract:{version.id}", {"version_id": version.id})
            enqueue(self.db, JobType.DOCUMENT_INDEXING, f"index:{doc.id}:{number}", {"document_id": doc.id})
            self.db.commit()
            return version
        except IntegrityError:
            self.db.rollback()
            if new_file is not None:
                self.files.discard(new_file)
            raise conflict("VERSION_CONFLICT", "A concurrent upload created this version number; retry.")
        except Exception:
            self.db.rollback()
            if new_file is not None:
                self.files.discard(new_file)
            raise

    def restore(self, ctx, document_id: int, version_id: int, change_description: str | None):
        doc = self.documents.get_visible(ctx, document_id)
        old = self.versions.get_in_document(doc.id, version_id)
        if old is None:
            raise not_found("INVALID_DOCUMENT_VERSION", "Version not found for this document.")
        text = change_description or f"Restored from version {old.version_number}"
        return self.create_version(ctx, document_id, None, text, True, restored_from=old, file_row=old.file)
