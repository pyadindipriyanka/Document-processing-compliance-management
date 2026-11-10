from app.core.enums import DocStatus
from app.core.errors import conflict, not_found
from app.core.types import utcnow
from app.models import DocumentFile
from app.repositories.repos import FileRepo
from app.services import audit
from app.services.documents import DocumentService
from app.services.retention import retention_blocks_deletion
from app.storage.local import LocalStorage


class FileService:
    def __init__(self, db, storage: LocalStorage | None = None):
        self.db = db
        self.storage = storage or LocalStorage()
        self.files, self.documents = FileRepo(db), DocumentService(db)

    def store_for_document(self, ctx, doc, upload) -> DocumentFile:
        """Validates + stores the bytes, then inserts the metadata row (caller commits).
        If anything after the write fails the caller must call `discard`."""
        stored = self.storage.save_upload(upload, ctx.org_id)
        row = DocumentFile(document_id=doc.id, organization_id=ctx.org_id, original_filename=stored.original_name,
                           storage_key=stored.key, content_type=stored.content_type, size_bytes=stored.size,
                           checksum_sha256=stored.checksum, uploaded_by=ctx.user.id)
        try:
            self.files.add(row)
        except Exception:
            self.storage.delete(stored.key)
            raise
        return row

    def discard(self, row: DocumentFile) -> None:
        self.storage.delete(row.storage_key)

    def upload_attachment(self, ctx, document_id: int, upload) -> DocumentFile:
        doc = self.documents.get_visible(ctx, document_id, lock=True)
        self.documents.require_modify(ctx, doc)
        if doc.status in (DocStatus.ARCHIVED, DocStatus.UNDER_REVIEW, DocStatus.SUBMITTED):
            raise conflict("DOCUMENT_NOT_EDITABLE", f"Files cannot be added while the document is {doc.status}.")
        row = self.store_for_document(ctx, doc, upload)
        try:
            audit.record(self.db, action="FILE_UPLOADED", entity_type="file", entity_id=row.id, actor_id=ctx.user.id,
                         org_id=ctx.org_id, meta={"document_id": doc.id, "size": row.size_bytes,
                                                  "content_type": row.content_type, "sha256": row.checksum_sha256})
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.discard(row)
            raise
        return row

    def list(self, ctx, document_id: int):
        doc = self.documents.get_visible(ctx, document_id)
        return self.files.list_for_document(doc.id)

    def get_for_download(self, ctx, file_id: int):
        row = self.files.get(file_id)
        if row is None or row.deleted_at is not None or row.organization_id != ctx.org_id:
            raise not_found("FILE_NOT_FOUND", "File not found.")
        doc = self.documents.get_visible(ctx, row.document_id)  # 403 DOCUMENT_ACCESS_DENIED when not allowed
        audit.record(self.db, action="FILE_DOWNLOADED", entity_type="file", entity_id=row.id, actor_id=ctx.user.id,
                     org_id=ctx.org_id, meta={"document_id": doc.id})
        self.db.commit()
        return row, self.storage.path_for(row.storage_key)

    def delete(self, ctx, file_id: int) -> None:
        row = self.files.get(file_id)
        if row is None or row.deleted_at is not None or row.organization_id != ctx.org_id:
            raise not_found("FILE_NOT_FOUND", "File not found.")
        doc = self.documents.get_visible(ctx, row.document_id, lock=True)
        self.documents.require_modify(ctx, doc)
        if self.files.is_version_file(row.id):
            raise conflict("FILE_IN_USE", "This file belongs to a document version and cannot be deleted.")
        if doc.status in (DocStatus.ARCHIVED, DocStatus.UNDER_REVIEW, DocStatus.SUBMITTED):
            raise conflict("DOCUMENT_NOT_EDITABLE", f"Files cannot be removed while the document is {doc.status}.")
        if retention_blocks_deletion(self.db, doc):
            raise conflict("DOCUMENT_RETENTION_ACTIVE", "A retention policy prohibits deleting this file.")
        row.deleted_at = utcnow()
        audit.record(self.db, action="FILE_DELETED", entity_type="file", entity_id=row.id, actor_id=ctx.user.id,
                     org_id=ctx.org_id, meta={"document_id": doc.id})
        self.db.commit()
        self.storage.delete(row.storage_key)
