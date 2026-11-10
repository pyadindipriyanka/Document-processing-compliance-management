from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_ctx, get_current_user, require_roles
from app.api.routes.common import make_page
from app.core.enums import Role
from app.db.session import get_db
from app.models import User
from app.repositories.repos import AuditRepo, JobRepo
from app.schemas.schemas import AuditLogOut, JobOut, NotificationOut
from app.services.access import Ctx
from app.services.jobs import drain_in_new_session, run_scheduled
from app.services.notifications import NotificationService

router = APIRouter(tags=["Notifications, audit & jobs"])


# ---------------- notifications ----------------
@router.get("/notifications")
def list_notifications(unread_only: bool = False, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items, total = NotificationService(db).list(user, unread_only, page, page_size)
    return make_page(items, total, page, page_size, NotificationOut)


@router.patch("/notifications/read-all")
def read_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"updated": NotificationService(db).mark_all_read(user)}


@router.patch("/notifications/{notification_id}/read", response_model=NotificationOut)
def read_one(notification_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return NotificationService(db).mark_read(user, notification_id)


# ---------------- audit logs (read-only; no write endpoints exist) ----------------
@router.get("/audit-logs")
def audit_logs(action: Optional[str] = None, entity_type: Optional[str] = None, entity_id: Optional[str] = None,
               actor_id: Optional[int] = None, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
               ctx: Ctx = Depends(require_roles(Role.ORG_ADMIN, Role.AUDITOR)), db: Session = Depends(get_db)):
    items, total = AuditRepo(db).search(ctx.org_id, action=action, entity_type=entity_type, entity_id=entity_id,
                                        actor_id=actor_id, page=page, page_size=page_size)
    rows = [AuditLogOut(id=i.id, actor_id=i.actor_id, organization_id=i.organization_id, action=i.action,
                        entity_type=i.entity_type, entity_id=i.entity_id, correlation_id=i.correlation_id,
                        metadata=i.details, created_at=i.created_at) for i in items]
    import math
    return {"items": [r.model_dump(mode="json") for r in rows], "total": total, "page": page, "page_size": page_size,
            "pages": math.ceil(total / page_size) if total else 0}


# ---------------- background jobs ----------------
@router.get("/jobs")
def list_jobs(status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
              ctx: Ctx = Depends(require_roles(Role.ORG_ADMIN)), db: Session = Depends(get_db)):
    items, total = JobRepo(db).list(status.upper() if status else None, page, page_size)
    return make_page(items, total, page, page_size, JobOut)


@router.post("/jobs/run-scheduled", status_code=202)
def trigger_scheduled(background_tasks: BackgroundTasks,
                      ctx: Ctx = Depends(require_roles(Role.ORG_ADMIN)), db: Session = Depends(get_db)):
    """Enqueues expiration scan, retention check, reminders and escalation (deduplicated per hour)."""
    ids = run_scheduled(db)
    background_tasks.add_task(drain_in_new_session)
    return {"enqueued_job_ids": ids}
