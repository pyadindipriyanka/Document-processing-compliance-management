import copy
from datetime import timedelta

from app.core.config import get_settings
from app.core.enums import DocStatus, HistoryDecision, NotificationType, RejectionPolicy, ReviewStatus, Role
from app.core.errors import conflict, forbidden, not_found, unprocessable
from app.core.types import utcnow
from app.models import ApprovalHistory, DocumentReview
from app.repositories.repos import CategoryRepo, DocumentRepo, MemberRepo, ReviewRepo, WorkflowRepo
from app.services import audit
from app.services.categories import validate_metadata
from app.services.documents import DocumentService
from app.services.notifications import notify
from app.services.transitions import transition
from app.services.workflows import APPROVER_ROLES, resolve_stage_reviewers


class ReviewService:
    def __init__(self, db):
        self.db = db
        self.reviews, self.docs = ReviewRepo(db), DocumentRepo(db)
        self.documents = DocumentService(db)

    # ------------------------------------------------------------------ helpers
    def _history(self, doc, decision, actor_id, comment=None, review=None, stage_order=None):
        version = review.version_number if review else doc.current_version
        row = ApprovalHistory(
            document_id=doc.id, version_number=version, submission_no=doc.submission_no,
            stage_order=review.stage_order if review else stage_order, review_id=review.id if review else None,
            actor_id=actor_id, decision=str(decision), comment=comment, metadata_snapshot=self.documents.snapshot(doc))
        self.db.add(row)
        return row

    @staticmethod
    def _stage(doc, order: int) -> dict:
        return next(s for s in doc.workflow_snapshot["stages"] if s["order"] == order)

    def _activate_stage(self, ctx_user_id, org_id, doc, order: int) -> None:
        stage = self._stage(doc, order)
        hours = stage.get("escalate_after_hours") or get_settings().REVIEW_ESCALATION_HOURS
        due = utcnow() + timedelta(hours=hours)
        for uid in stage["reviewer_ids"]:
            review = DocumentReview(document_id=doc.id, version_number=doc.current_version,
                                    submission_no=doc.submission_no, stage_order=order, stage_name=stage["name"],
                                    reviewer_id=uid, due_at=due)
            self.reviews.add(review)
            notify(self.db, user_id=uid, org_id=org_id, type_=NotificationType.REVIEW_ASSIGNED,
                   title=f"Review requested: {doc.title}", message=f"Stage '{stage['name']}' awaits your decision.",
                   entity_type="review", entity_id=review.id, dedupe_key=f"assigned:{review.id}")
            audit.record(self.db, action="REVIEW_ASSIGNED", entity_type="review", entity_id=review.id,
                         actor_id=ctx_user_id, org_id=org_id,
                         meta={"document_id": doc.id, "reviewer_id": uid, "stage": order})
        doc.current_stage = order

    def _cancel_pending(self, doc) -> None:
        for r in self.reviews.pending_for_document(doc.id):
            r.status, r.decided_at = ReviewStatus.CANCELLED.value, utcnow()

    # ------------------------------------------------------------------ submit
    def submit(self, ctx, document_id: int, allow_expired: bool = False):
        doc = self.documents.get_visible(ctx, document_id, lock=True)
        self.documents.require_modify(ctx, doc)
        if doc.status == DocStatus.ARCHIVED:
            raise conflict("DOCUMENT_ARCHIVED", "Archived documents must be restored before submission.")
        if doc.status == DocStatus.EXPIRED and not (allow_expired and ctx.is_manager):
            raise conflict("DOCUMENT_EXPIRED", "Expired documents can only be submitted by a manager who explicitly "
                                               "sets allow_expired=true (or renew the document).")
        if doc.status not in (DocStatus.DRAFT, DocStatus.REJECTED, DocStatus.EXPIRED):
            raise conflict("INVALID_WORKFLOW_TRANSITION", f"A document in status {doc.status} cannot be submitted.")
        if doc.current_version < 1:
            raise conflict("INVALID_DOCUMENT_VERSION", "Upload a document version before submitting.")
        cat = CategoryRepo(self.db).get(doc.category_id)
        validate_metadata(cat.definitions, self.documents.metadata_of(doc))  # REQUIRED_METADATA_MISSING

        wf = next((w for w in WorkflowRepo(self.db).active_list(ctx.org_id)
                   if doc.category_id in (w.category_ids or [])), None)
        if wf is None:
            raise conflict("WORKFLOW_NOT_FOUND", "No active workflow is configured for this document's category.")
        stages = []
        for st in wf.stages:
            ids = resolve_stage_reviewers(self.db, ctx.org_id, st, owner_id=doc.owner_id)
            if not ids:
                raise conflict("WORKFLOW_NOT_CONFIGURED",
                               f"Stage '{st.name}' has no eligible reviewers (the owner cannot review their own document).")
            required = len(ids) if st.require_all else min(st.required_approvals, len(ids))
            stages.append({"order": st.stage_order, "name": st.name, "reviewer_ids": ids, "required": required,
                           "require_all": st.require_all, "escalate_after_hours": st.escalate_after_hours,
                           "escalate_to_user_id": st.escalate_to_user_id})
        transition(doc, DocStatus.SUBMITTED)
        doc.submission_no += 1
        doc.workflow_id = wf.id
        doc.workflow_snapshot = {"workflow_id": wf.id, "workflow_name": wf.name, "workflow_version": wf.version,
                                 "rejection_policy": wf.rejection_policy, "stages": stages}
        self._history(doc, HistoryDecision.SUBMITTED, ctx.user.id, stage_order=None)
        transition(doc, DocStatus.UNDER_REVIEW)
        self._activate_stage(ctx.user.id, ctx.org_id, doc, 1)
        notify(self.db, user_id=doc.owner_id, org_id=ctx.org_id, type_=NotificationType.DOCUMENT_SUBMITTED,
               title=f"Submitted: {doc.title}", message="Your document entered the approval workflow.",
               entity_type="document", entity_id=doc.id, dedupe_key=f"submitted:{doc.id}:{doc.submission_no}")
        audit.record(self.db, action="DOCUMENT_SUBMITTED", entity_type="document", entity_id=doc.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta={"workflow_id": wf.id, "workflow_version": wf.version, "submission_no": doc.submission_no})
        self.db.commit()
        return doc

    # ------------------------------------------------------------------ reads
    def pending(self, ctx, show_all: bool, page: int, page_size: int):
        show_all = show_all and (ctx.is_manager or ctx.is_read_only)
        return self.reviews.pending_for_user(ctx, show_all, page, page_size)

    def get(self, ctx, review_id: int):
        review = self.reviews.get(review_id)
        doc = self.docs.get_in_org(ctx.org_id, review.document_id) if review else None
        if review is None or doc is None:
            raise not_found("REVIEW_NOT_FOUND", "Review not found.")
        if review.reviewer_id != ctx.user.id:
            self.documents.get_visible(ctx, doc.id)
        return review

    # ------------------------------------------------------------------ decisions
    def _load_for_decision(self, ctx, review_id: int):
        review = self.reviews.get(review_id)
        doc0 = self.docs.get_in_org(ctx.org_id, review.document_id) if review else None
        if review is None or doc0 is None:
            raise not_found("REVIEW_NOT_FOUND", "Review not found.")
        if ctx.is_read_only or review.reviewer_id != ctx.user.id:
            raise forbidden("REVIEW_ACCESS_DENIED", "Only the assigned reviewer can decide this review.")
        doc = self.docs.lock(doc0.id)  # serialises concurrent decisions on the same document
        self.db.refresh(review)  # re-read under the lock so we never act on a stale status
        if review.status != ReviewStatus.PENDING:
            raise conflict("REVIEW_ALREADY_COMPLETED", "This review has already been completed.")
        if (doc.status != DocStatus.UNDER_REVIEW or review.submission_no != doc.submission_no
                or review.stage_order != doc.current_stage):
            raise conflict("INVALID_WORKFLOW_TRANSITION", "This review is not active for the document's current stage.")
        return review, doc

    def approve(self, ctx, review_id: int, comment: str | None):
        review, doc = self._load_for_decision(ctx, review_id)
        review.status, review.comment, review.decided_at = ReviewStatus.APPROVED.value, comment, utcnow()
        self._history(doc, HistoryDecision.APPROVED, ctx.user.id, comment, review)
        audit.record(self.db, action="REVIEW_APPROVED", entity_type="review", entity_id=review.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"document_id": doc.id, "stage": review.stage_order})
        self.db.flush()
        stage = self._stage(doc, review.stage_order)
        approved = sum(1 for r in self.reviews.for_document_stage(doc.id, doc.submission_no, review.stage_order)
                       if r.status == ReviewStatus.APPROVED)
        if approved >= stage["required"]:
            for r in self.reviews.pending_for_document(doc.id):
                if r.stage_order == review.stage_order:
                    r.status, r.decided_at = ReviewStatus.CANCELLED.value, utcnow()
            self._history(doc, HistoryDecision.STAGE_COMPLETED, ctx.user.id, None, review)
            nxt = review.stage_order + 1
            if any(s["order"] == nxt for s in doc.workflow_snapshot["stages"]):
                self._activate_stage(ctx.user.id, ctx.org_id, doc, nxt)
            else:
                transition(doc, DocStatus.APPROVED)
                doc.current_stage = None
                self._history(doc, HistoryDecision.FINAL_APPROVED, ctx.user.id, None, review)
                notify(self.db, user_id=doc.owner_id, org_id=ctx.org_id, type_=NotificationType.APPROVAL_COMPLETED,
                       title=f"Approved: {doc.title}", message="All approval stages are complete.",
                       entity_type="document", entity_id=doc.id,
                       dedupe_key=f"approved:{doc.id}:{doc.submission_no}")
                audit.record(self.db, action="DOCUMENT_APPROVED", entity_type="document", entity_id=doc.id,
                             actor_id=ctx.user.id, org_id=ctx.org_id, meta={"version": review.version_number})
        self.db.commit()
        return review

    def reject(self, ctx, review_id: int, comment: str | None):
        comment = self._require_comment(comment)
        review, doc = self._load_for_decision(ctx, review_id)
        review.status, review.comment, review.decided_at = ReviewStatus.REJECTED.value, comment, utcnow()
        self._history(doc, HistoryDecision.REJECTED, ctx.user.id, comment, review)
        audit.record(self.db, action="REVIEW_REJECTED", entity_type="review", entity_id=review.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"document_id": doc.id, "stage": review.stage_order})
        self.db.flush()
        reject_document = True
        if doc.workflow_snapshot["rejection_policy"] == RejectionPolicy.STAGE_QUORUM:
            stage = self._stage(doc, review.stage_order)
            rows = self.reviews.for_document_stage(doc.id, doc.submission_no, review.stage_order)
            reachable = sum(1 for r in rows if r.status in (ReviewStatus.APPROVED, ReviewStatus.PENDING))
            reject_document = reachable < stage["required"]
        if reject_document:
            self._cancel_pending(doc)
            transition(doc, DocStatus.REJECTED)
            doc.current_stage = None
            notify(self.db, user_id=doc.owner_id, org_id=ctx.org_id, type_=NotificationType.DOCUMENT_REJECTED,
                   title=f"Rejected: {doc.title}", message=comment, entity_type="document", entity_id=doc.id,
                   dedupe_key=f"rejected:{doc.id}:{doc.submission_no}")
            audit.record(self.db, action="DOCUMENT_REJECTED", entity_type="document", entity_id=doc.id,
                         actor_id=ctx.user.id, org_id=ctx.org_id, meta={"review_id": review.id})
        self.db.commit()
        return review

    def request_changes(self, ctx, review_id: int, comment: str | None):
        comment = self._require_comment(comment)
        review, doc = self._load_for_decision(ctx, review_id)
        review.status, review.comment, review.decided_at = ReviewStatus.CHANGES_REQUESTED.value, comment, utcnow()
        self._history(doc, HistoryDecision.CHANGES_REQUESTED, ctx.user.id, comment, review)
        self.db.flush()
        self._cancel_pending(doc)
        transition(doc, DocStatus.DRAFT)  # owner edits / uploads a new version and resubmits
        doc.current_stage = None
        notify(self.db, user_id=doc.owner_id, org_id=ctx.org_id, type_=NotificationType.CHANGES_REQUESTED,
               title=f"Changes requested: {doc.title}", message=comment, entity_type="document", entity_id=doc.id,
               dedupe_key=f"changes:{review.id}")
        audit.record(self.db, action="REVIEW_CHANGES_REQUESTED", entity_type="review", entity_id=review.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"document_id": doc.id})
        self.db.commit()
        return review

    @staticmethod
    def _require_comment(comment: str | None) -> str:
        if not comment or not comment.strip():
            raise unprocessable("COMMENT_REQUIRED", "A comment is required for this decision.")
        return comment.strip()

    # ------------------------------------------------------------------ reassignment
    def reassign(self, ctx, review_id: int, new_reviewer_id: int, reason: str | None):
        if not ctx.is_manager:
            raise forbidden("INSUFFICIENT_ROLE", "Only managers and admins can reassign reviews.")
        review = self.reviews.get(review_id)
        doc0 = self.docs.get_in_org(ctx.org_id, review.document_id) if review else None
        if review is None or doc0 is None:
            raise not_found("REVIEW_NOT_FOUND", "Review not found.")
        doc = self.docs.lock(doc0.id)
        self.db.refresh(review)
        if review.status != ReviewStatus.PENDING:
            raise conflict("REVIEW_ALREADY_COMPLETED", "This review has already been completed.")
        member = MemberRepo(self.db).get_pair(ctx.org_id, new_reviewer_id)
        if member is None or not member.is_active or member.role not in APPROVER_ROLES:
            raise unprocessable("INVALID_REVIEWER", "The new reviewer must be an active member who can approve.")
        if new_reviewer_id == doc.owner_id:
            raise unprocessable("INVALID_REVIEWER", "The document owner cannot review their own document.")
        siblings = self.reviews.for_document_stage(doc.id, review.submission_no, review.stage_order)
        if any(r.reviewer_id == new_reviewer_id and r.status in (ReviewStatus.PENDING, ReviewStatus.APPROVED)
               for r in siblings):
            raise conflict("DUPLICATE_REVIEWER", "That user already reviews this stage.")
        review.status, review.comment, review.decided_at = ReviewStatus.REASSIGNED.value, reason, utcnow()
        fresh = self.reviews.add(DocumentReview(
            document_id=doc.id, version_number=review.version_number, submission_no=review.submission_no,
            stage_order=review.stage_order, stage_name=review.stage_name, reviewer_id=new_reviewer_id,
            due_at=review.due_at))
        snap = copy.deepcopy(doc.workflow_snapshot)
        stage = next(s for s in snap["stages"] if s["order"] == review.stage_order)
        stage["reviewer_ids"] = [new_reviewer_id if i == review.reviewer_id else i for i in stage["reviewer_ids"]]
        doc.workflow_snapshot = snap
        self._history(doc, HistoryDecision.REASSIGNED, ctx.user.id, reason, fresh)
        notify(self.db, user_id=new_reviewer_id, org_id=ctx.org_id, type_=NotificationType.REVIEW_ASSIGNED,
               title=f"Review requested: {doc.title}", message=f"Stage '{review.stage_name}' awaits your decision.",
               entity_type="review", entity_id=fresh.id, dedupe_key=f"assigned:{fresh.id}")
        audit.record(self.db, action="REVIEW_REASSIGNED", entity_type="review", entity_id=fresh.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id,
                     meta={"from": review.reviewer_id, "to": new_reviewer_id, "reason": reason})
        self.db.commit()
        return fresh
