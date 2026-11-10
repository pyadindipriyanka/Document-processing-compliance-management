from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.api.deps import get_ctx, require_roles
from app.core.enums import Role
from app.db.session import get_db
from app.schemas.schemas import CategoryIn, CategoryOut, CategoryPatch
from app.services.access import Ctx
from app.services.categories import CategoryService

router = APIRouter(prefix="/categories", tags=["Categories & metadata"])
admin = require_roles(Role.ORG_ADMIN)


@router.post("", response_model=CategoryOut, status_code=201)
def create(body: CategoryIn, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return CategoryService(db).create(ctx, body)


@router.get("", response_model=list[CategoryOut])
def list_categories(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return CategoryService(db).repo.list(ctx.org_id)


@router.patch("/{category_id}", response_model=CategoryOut)
def update(category_id: int, body: CategoryPatch, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return CategoryService(db).update(ctx, category_id, body)


@router.delete("/{category_id}", status_code=204)
def delete(category_id: int, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    CategoryService(db).delete(ctx, category_id)
    return Response(status_code=204)
