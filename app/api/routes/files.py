import os

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import drain_jobs_after_response, get_ctx
from app.core.errors import not_found
from app.db.session import get_db
from app.schemas.schemas import FileOut, RestoreVersionIn, VersionOut
from app.services.access import Ctx
from app.services.files import FileService
from app.services.versions import VersionService

router = APIRouter(tags=["Files & versions"], dependencies=[Depends(drain_jobs_after_response)])


# ---------------- files ----------------
@router.post("/documents/{document_id}/files", response_model=FileOut, status_code=201)
def upload_file(document_id: int, file: UploadFile = File(...), ctx: Ctx = Depends(get_ctx),
                db: Session = Depends(get_db)):
    return FileService(db).upload_attachment(ctx, document_id, file)


@router.get("/documents/{document_id}/files", response_model=list[FileOut])
def list_files(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return FileService(db).list(ctx, document_id)


@router.get("/files/{file_id}/download")
def download_file(file_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    row, path = FileService(db).get_for_download(ctx, file_id)
    if not os.path.exists(path):
        raise not_found("FILE_NOT_FOUND", "The stored file is missing.")
    return FileResponse(path, media_type=row.content_type, filename=row.original_filename,
                        headers={"X-Content-Type-Options": "nosniff"})


@router.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    FileService(db).delete(ctx, file_id)
    return Response(status_code=204)


# ---------------- versions ----------------
@router.post("/documents/{document_id}/versions", response_model=VersionOut, status_code=201)
def create_version(document_id: int, file: UploadFile = File(...), change_description: str = Form(""),
                   is_material: bool = Form(True), ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return VersionService(db).create_version(ctx, document_id, file, change_description or None, is_material)


@router.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def list_versions(document_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return VersionService(db).list(ctx, document_id)


@router.get("/documents/{document_id}/versions/{version_id}", response_model=VersionOut)
def get_version(document_id: int, version_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return VersionService(db).get(ctx, document_id, version_id)


@router.post("/documents/{document_id}/versions/{version_id}/restore", response_model=VersionOut, status_code=201)
def restore_version(document_id: int, version_id: int, body: RestoreVersionIn | None = None,
                    ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return VersionService(db).restore(ctx, document_id, version_id, body.change_description if body else None)
