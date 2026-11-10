from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_ctx, require_roles
from app.core.enums import Role
from app.db.session import get_db
from app.schemas.schemas import WorkflowIn, WorkflowOut, WorkflowPatch
from app.services.access import Ctx
from app.services.workflows import WorkflowService, workflow_to_dict

router = APIRouter(prefix="/workflows", tags=["Workflows"])
admin = require_roles(Role.ORG_ADMIN)


@router.post("", response_model=WorkflowOut, status_code=201)
def create(body: WorkflowIn, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return workflow_to_dict(WorkflowService(db).create(ctx, body))


@router.get("", response_model=list[WorkflowOut])
def list_workflows(ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return [workflow_to_dict(w) for w in WorkflowService(db).list(ctx)]


@router.get("/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return workflow_to_dict(WorkflowService(db).get(ctx, workflow_id))


@router.patch("/{workflow_id}", response_model=WorkflowOut)
def update(workflow_id: int, body: WorkflowPatch, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return workflow_to_dict(WorkflowService(db).update(ctx, workflow_id, body))


@router.post("/{workflow_id}/activate", response_model=WorkflowOut)
def activate(workflow_id: int, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return workflow_to_dict(WorkflowService(db).set_active(ctx, workflow_id, True))


@router.post("/{workflow_id}/deactivate", response_model=WorkflowOut)
def deactivate(workflow_id: int, ctx: Ctx = Depends(admin), db: Session = Depends(get_db)):
    return workflow_to_dict(WorkflowService(db).set_active(ctx, workflow_id, False))
