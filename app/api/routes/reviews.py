from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import drain_jobs_after_response, get_ctx
from app.api.routes.common import make_page
from app.db.session import get_db
from app.schemas.schemas import DecisionIn, ReassignIn, ReviewOut
from app.services.access import Ctx
from app.services.reviews import ReviewService

router = APIRouter(prefix="/reviews", tags=["Reviews"], dependencies=[Depends(drain_jobs_after_response)])


@router.get("/pending")
def pending(all: bool = Query(False, description="Managers/admins/auditors: list every pending review in the org"),
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    items, total = ReviewService(db).pending(ctx, all, page, page_size)
    return make_page(items, total, page, page_size, ReviewOut)


@router.get("/{review_id}", response_model=ReviewOut)
def get_review(review_id: int, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return ReviewService(db).get(ctx, review_id)


@router.post("/{review_id}/approve", response_model=ReviewOut)
def approve(review_id: int, body: DecisionIn | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return ReviewService(db).approve(ctx, review_id, body.comment if body else None)


@router.post("/{review_id}/reject", response_model=ReviewOut)
def reject(review_id: int, body: DecisionIn | None = None, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return ReviewService(db).reject(ctx, review_id, body.comment if body else None)


@router.post("/{review_id}/request-changes", response_model=ReviewOut)
def request_changes(review_id: int, body: DecisionIn | None = None, ctx: Ctx = Depends(get_ctx),
                    db: Session = Depends(get_db)):
    return ReviewService(db).request_changes(ctx, review_id, body.comment if body else None)


@router.post("/{review_id}/reassign", response_model=ReviewOut)
def reassign(review_id: int, body: ReassignIn, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    return ReviewService(db).reassign(ctx, review_id, body.new_reviewer_id, body.reason)
