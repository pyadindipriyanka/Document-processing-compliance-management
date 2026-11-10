from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import drain_jobs_after_response, get_ctx
from app.api.routes.common import make_page
from app.core.enums import DocStatus
from app.core.errors import not_found, unprocessable
from app.db.session import get_db
from app.schemas.schemas import (ApprovalHistoryOut, DocumentDetailOut, DocumentIn, DocumentOut, DocumentPatch,
                                 MetadataOut, MetadataPatch, RenewIn, SubmitIn)
from app.services import audit
from app.services.access import Ctx
from app.services.documents import DocumentService
from app.services.reviews import ReviewService
from app.storage.local import LocalStorage

router = APIRouter(prefix="/documents", tags=["Documents"], dependencies=[Depends(drain_jobs_after_response)])


def _detail(doc, metadata) -> DocumentDetailOut:
    return DocumentDetailOut(**DocumentOut.model_validate(doc).model_dump(), metadata=metadata)


@router.post("", response_model=DocumentDetailOut, status_code=201)
def create_document(body: DocumentIn, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    svc = DocumentService(db)
    doc = svc.create(ctx, body)
    return _detail(doc, svc.metadata_of(doc))


@router.get("")
def list_documents(
    search: Optional[str] = Query(None, max_length=200), category_id: Optional[int] = None,
    status: Optional[str] = None, owner_id: Optional[int] = None, from_date: Optional[date] = None,
    to_date: Optional[date] = None, expires_before: Optional[date] = None, expires_after: Optional[date] = None,
    include_archived: bool = False, sort_by: str = Query("created_at", pattern="^(created_at|updated_at|title|expiration_date|status|id)$"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$"), page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100), ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db),
):
    if status:
        status = status.upper()
        if status not in {s.value for s in DocStatus}:
            raise unprocessable("INVALID_STATUS", f"Unknown status '{status}'.")
    items, total = DocumentService(db).search(
        ctx, q=search, category_id=category_id, status=status, owner_id=owner_id, from_date=from_date,
        to_date=to_date, expires_before=expires_before, expires_after=expires_after,
        include_archived=include_archived, sort_by=sort_by, sort_order=sort_order, page=page, page_size=page_size)
    return make_page(items, total, page, page_size, DocumentOut)


# NOTE: static paths must be declared before /{document_id}
@router.get("/expiring")
def expiring(days: int = Query(30, ge=1, le=3650), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
             ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    items, total = DocumentService(db).expiring(ctx, days, page, page_size)
    return make_page(items, total, page, page_size, DocumentOut)


@router.get("/expired")
def expired(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    items, total = DocumentService(db).expired(ctx, page, page_size)
    return make_page(items, total, page, page_size, DocumentOut)


@router.get("/{document_id}", response_model=DocumentDetailOut)
def get_document(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    doc, md = DocumentService(db).detail(ctx, document_id)
    return _detail(doc, md)


@router.patch("/{document_id}", response_model=DocumentDetailOut)
def update_document(document_id: int, body: DocumentPatch, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    svc = DocumentService(db)
    doc = svc.update(ctx, document_id, body)
    return _detail(doc, svc.metadata_of(doc))


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    DocumentService(db).delete(ctx, document_id)
    return Response(status_code=204)


@router.post("/{document_id}/archive", response_model=DocumentOut)
def archive(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return DocumentService(db).archive(ctx, document_id)


@router.post("/{document_id}/restore", response_model=DocumentOut)
def restore(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return DocumentService(db).restore(ctx, document_id)


@router.post("/{document_id}/renew", response_model=DocumentOut)
def renew(document_id: int, body: RenewIn, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return DocumentService(db).renew(ctx, document_id, body.expiration_date, body.comment)


@router.get("/{document_id}/download")
def download(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    doc, file = DocumentService(db).current_file(ctx, document_id)
    path = LocalStorage().path_for(file.storage_key)
    if not path.exists():
        raise not_found("FILE_NOT_FOUND", "The stored file is missing.")
    audit.record(db, action="FILE_DOWNLOADED", entity_type="file", entity_id=file.id, actor_id=ctx.user.id,
                 org_id=ctx.org_id, meta={"document_id": doc.id, "via": "document"})
    db.commit()
    return FileResponse(path, media_type=file.content_type, filename=file.original_filename,
                        headers={"X-Content-Type-Options": "nosniff"})


@router.get("/{document_id}/metadata", response_model=MetadataOut)
def get_metadata(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return DocumentService(db).metadata(ctx, document_id)


@router.patch("/{document_id}/metadata", response_model=MetadataOut)
def patch_metadata(document_id: int, body: MetadataPatch, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    svc = DocumentService(db)
    svc.update_metadata(ctx, document_id, body.metadata)
    return svc.metadata(ctx, document_id)


@router.post("/{document_id}/submit", response_model=DocumentOut)
def submit(document_id: int, body: SubmitIn | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return ReviewService(db).submit(ctx, document_id, allow_expired=bool(body and body.allow_expired))


@router.get("/{document_id}/approval-history", response_model=list[ApprovalHistoryOut])
def approval_history(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return DocumentService(db).history(ctx, document_id)
