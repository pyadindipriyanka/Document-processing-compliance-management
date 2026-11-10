from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_ctx, require_roles
from app.core.enums import Role
from app.db.session import get_db
from app.schemas.schemas import RetentionIn, RetentionOut, RetentionPatch
from app.services.access import Ctx
from app.services.retention import RetentionService

router = APIRouter(prefix="/retention-policies", tags=["Retention"])
admin = require_roles(Role.ORG_ADMIN)


@router.post("", response_model=RetentionOut, status_code=201)
def create(body: RetentionIn, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return RetentionService(db).create(ctx, body)


@router.get("", response_model=list[RetentionOut])
def list_policies(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return RetentionService(db).list(ctx)


@router.patch("/{policy_id}", response_model=RetentionOut)
def update(policy_id: int, body: RetentionPatch, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return RetentionService(db).update(ctx, policy_id, body)
