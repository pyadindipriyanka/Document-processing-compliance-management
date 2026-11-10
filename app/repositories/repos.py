"""Repositories: all SQL lives here. Services never build queries themselves."""
from datetime import date, datetime, time, timedelta, timezone
from typing import Generic, TypeVar

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.enums import AccessLevel, DocStatus, JobStatus, ReviewStatus, Role
from app.core.types import utcnow
from app.models import (ApprovalHistory, AuditLog, BackgroundJob, Document, DocumentAccess, DocumentCategory,
                        DocumentFile, DocumentMetadata, DocumentReview, DocumentVersion, Notification,
                        Organization, OrganizationMember, RefreshToken, RetentionPolicy, User, Workflow)
from app.services.access import Ctx

T = TypeVar("T")


def paginate(db: Session, stmt, page: int, page_size: int):
    total = db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()
    items = db.execute(stmt.limit(page_size).offset((page - 1) * page_size)).scalars().unique().all()
    return items, total


class BaseRepo(Generic[T]):
    model: type

    def __init__(self, db: Session):
        self.db = db

    def get(self, id_: int):
        return self.db.get(self.model, id_)

    def get_for_update(self, id_: int):
        stmt = select(self.model).where(self.model.id == id_).with_for_update(of=self.model).execution_options(
            populate_existing=True)
        return self.db.execute(stmt).unique().scalar_one_or_none()

    def add(self, obj):
        self.db.add(obj)
        self.db.flush()
        return obj


class UserRepo(BaseRepo):
    model = User

    def by_email(self, email: str):
        return self.db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()


class TokenRepo(BaseRepo):
    model = RefreshToken

    def by_jti_for_update(self, jti: str):
        return self.db.execute(select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
                               ).scalar_one_or_none()

    def session_active(self, family_id: str) -> bool:
        stmt = select(RefreshToken.id).where(
            RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > utcnow()).limit(1)
        return self.db.execute(stmt).first() is not None

    def revoke_family(self, family_id: str) -> None:
        for t in self.db.execute(select(RefreshToken).where(
                RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))).scalars():
            t.revoked_at = utcnow()

    def revoke_all_for_user(self, user_id: int) -> None:
        for t in self.db.execute(select(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))).scalars():
            t.revoked_at = utcnow()


class OrgRepo(BaseRepo):
    model = Organization

    def by_name(self, name: str):
        return self.db.execute(select(Organization).where(func.lower(Organization.name) == name.lower())
                               ).scalar_one_or_none()

    def list_for(self, user: User):
        stmt = select(Organization).order_by(Organization.id)
        if not user.is_super_admin:
            stmt = stmt.join(OrganizationMember, OrganizationMember.organization_id == Organization.id).where(
                OrganizationMember.user_id == user.id, OrganizationMember.is_active.is_(True))
        return self.db.execute(stmt).scalars().all()


class MemberRepo(BaseRepo):
    model = OrganizationMember

    def get_pair(self, org_id: int, user_id: int):
        return self.db.execute(select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.user_id == user_id)
        ).unique().scalar_one_or_none()

    def active_for_user(self, user_id: int):
        return self.db.execute(select(OrganizationMember).where(
            OrganizationMember.user_id == user_id, OrganizationMember.is_active.is_(True))
        ).unique().scalars().all()

    def list(self, org_id: int):
        return self.db.execute(select(OrganizationMember).where(OrganizationMember.organization_id == org_id)
                               .order_by(OrganizationMember.id)).unique().scalars().all()

    def active_admin_count(self, org_id: int) -> int:
        return self.db.execute(select(func.count()).select_from(OrganizationMember).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.role == Role.ORG_ADMIN.value,
            OrganizationMember.is_active.is_(True))).scalar_one()

    def active_users_with_roles(self, org_id: int, roles: list[str]):
        return self.db.execute(select(OrganizationMember).join(User, User.id == OrganizationMember.user_id).where(
            OrganizationMember.organization_id == org_id, OrganizationMember.is_active.is_(True),
            User.is_active.is_(True), OrganizationMember.role.in_(roles))).unique().scalars().all()


class CategoryRepo(BaseRepo):
    model = DocumentCategory

    def get_in_org(self, org_id: int, id_: int):
        return self.db.execute(select(DocumentCategory).where(
            DocumentCategory.id == id_, DocumentCategory.organization_id == org_id)).unique().scalar_one_or_none()

    def by_name(self, org_id: int, name: str):
        return self.db.execute(select(DocumentCategory).where(
            DocumentCategory.organization_id == org_id, func.lower(DocumentCategory.name) == name.lower())
        ).unique().scalar_one_or_none()

    def list(self, org_id: int):
        return self.db.execute(select(DocumentCategory).where(DocumentCategory.organization_id == org_id)
                               .order_by(DocumentCategory.id)).unique().scalars().all()

    def document_count(self, category_id: int) -> int:
        return self.db.execute(select(func.count()).select_from(Document).where(
            Document.category_id == category_id)).scalar_one()


def visible_clause(ctx: Ctx):
    """Single source of truth for 'which documents may this user see' (used by detail, list, search, download)."""
    base = and_(Document.organization_id == ctx.org_id, Document.deleted_at.is_(None))
    if ctx.is_admin:
        return base
    uid = ctx.user.id
    granted = exists().where(DocumentAccess.document_id == Document.id, DocumentAccess.user_id == uid)
    assigned = exists().where(DocumentReview.document_id == Document.id, DocumentReview.reviewer_id == uid)
    levels = [AccessLevel.ORGANIZATION.value]
    sees_drafts = ctx.role in (Role.DOCUMENT_MANAGER, Role.AUDITOR)
    if sees_drafts:
        levels.append(AccessLevel.RESTRICTED.value)
    level_ok = Document.access_level.in_(levels)
    if not sees_drafts:  # other people's drafts stay private to employees / reviewers
        level_ok = and_(level_ok, Document.status != DocStatus.DRAFT.value)
    return and_(base, or_(Document.owner_id == uid, granted, assigned, level_ok))


SORTABLE = {"created_at": Document.created_at, "updated_at": Document.updated_at, "title": Document.title,
            "expiration_date": Document.expiration_date, "status": Document.status, "id": Document.id}


class DocumentRepo(BaseRepo):
    model = Document

    def get_in_org(self, org_id: int, id_: int):
        return self.db.execute(select(Document).where(
            Document.id == id_, Document.organization_id == org_id, Document.deleted_at.is_(None))
        ).unique().scalar_one_or_none()

    def lock_in_org(self, org_id: int, id_: int):
        return self.db.execute(select(Document).where(
            Document.id == id_, Document.organization_id == org_id, Document.deleted_at.is_(None)
        ).with_for_update(of=Document).execution_options(populate_existing=True)).unique().scalar_one_or_none()

    def lock(self, id_: int):
        return self.get_for_update(id_)

    def is_visible(self, ctx: Ctx, id_: int) -> bool:
        return self.db.execute(select(Document.id).where(Document.id == id_, visible_clause(ctx))).first() is not None

    def search(self, ctx: Ctx, *, q=None, category_id=None, status=None, owner_id=None, from_date: date | None = None,
               to_date: date | None = None, expires_before: date | None = None, expires_after: date | None = None,
               include_archived=False, sort_by="created_at", sort_order="desc", page=1, page_size=20):
        stmt = select(Document).where(visible_clause(ctx))
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            cond = Document.search_text.ilike(f"%{esc}%", escape="\\")
            if self.db.bind.dialect.name == "postgresql":
                cond = or_(cond, func.to_tsvector("english", func.coalesce(Document.search_text, "")).op("@@")(
                    func.plainto_tsquery("english", q)))
            stmt = stmt.where(cond)
        if category_id:
            stmt = stmt.where(Document.category_id == category_id)
        if status:
            stmt = stmt.where(Document.status == status)
        elif not include_archived:
            stmt = stmt.where(Document.status != DocStatus.ARCHIVED.value)
        if owner_id:
            stmt = stmt.where(Document.owner_id == owner_id)
        if from_date:
            stmt = stmt.where(Document.created_at >= datetime.combine(from_date, time.min, tzinfo=timezone.utc))
        if to_date:  # inclusive of the whole end day
            stmt = stmt.where(Document.created_at < datetime.combine(to_date + timedelta(days=1), time.min,
                                                                     tzinfo=timezone.utc))
        if expires_before:
            stmt = stmt.where(Document.expiration_date <= expires_before)
        if expires_after:
            stmt = stmt.where(Document.expiration_date >= expires_after)
        col = SORTABLE.get(sort_by, Document.created_at)
        stmt = stmt.order_by(col.asc() if sort_order == "asc" else col.desc(), Document.id.desc())
        return paginate(self.db, stmt, page, page_size)

    def approved_expiring_between(self, start: date, end: date):
        return self.db.execute(select(Document).where(
            Document.status == DocStatus.APPROVED.value, Document.deleted_at.is_(None),
            Document.expiration_date.is_not(None), Document.expiration_date >= start,
            Document.expiration_date <= end)).unique().scalars().all()

    def approved_past_expiry(self, today: date):
        return self.db.execute(select(Document).where(
            Document.status == DocStatus.APPROVED.value, Document.deleted_at.is_(None),
            Document.expiration_date.is_not(None), Document.expiration_date < today)).unique().scalars().all()

    def expired_docs(self):
        return self.db.execute(select(Document).where(
            Document.status == DocStatus.EXPIRED.value, Document.deleted_at.is_(None))).unique().scalars().all()

    def all_ids(self):
        return self.db.execute(select(Document.id).where(Document.deleted_at.is_(None))).scalars().all()

    def metadata_row(self, document_id: int):
        return self.db.execute(select(DocumentMetadata).where(DocumentMetadata.document_id == document_id)
                               ).scalar_one_or_none()

    def grants(self, document_id: int) -> list[int]:
        return list(self.db.execute(select(DocumentAccess.user_id).where(
            DocumentAccess.document_id == document_id)).scalars())

    def set_grants(self, document_id: int, user_ids: list[int]) -> None:
        for g in self.db.execute(select(DocumentAccess).where(DocumentAccess.document_id == document_id)).scalars():
            self.db.delete(g)
        self.db.flush()
        for uid in sorted(set(user_ids)):
            self.db.add(DocumentAccess(document_id=document_id, user_id=uid))


class FileRepo(BaseRepo):
    model = DocumentFile

    def list_for_document(self, document_id: int):
        return self.db.execute(select(DocumentFile).where(
            DocumentFile.document_id == document_id, DocumentFile.deleted_at.is_(None))
            .order_by(DocumentFile.id)).scalars().all()

    def is_version_file(self, file_id: int) -> bool:
        return self.db.execute(select(DocumentVersion.id).where(DocumentVersion.file_id == file_id)
                               .limit(1)).first() is not None


class VersionRepo(BaseRepo):
    model = DocumentVersion

    def list(self, document_id: int):
        return self.db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                               .order_by(DocumentVersion.version_number)).unique().scalars().all()

    def get_in_document(self, document_id: int, version_id: int):
        return self.db.execute(select(DocumentVersion).where(
            DocumentVersion.id == version_id, DocumentVersion.document_id == document_id)
        ).unique().scalar_one_or_none()

    def by_number(self, document_id: int, number: int):
        return self.db.execute(select(DocumentVersion).where(
            DocumentVersion.document_id == document_id, DocumentVersion.version_number == number)
        ).unique().scalar_one_or_none()

    def max_number(self, document_id: int) -> int:
        return self.db.execute(select(func.coalesce(func.max(DocumentVersion.version_number), 0)).where(
            DocumentVersion.document_id == document_id)).scalar_one()


class WorkflowRepo(BaseRepo):
    model = Workflow

    def get_in_org(self, org_id: int, id_: int):
        return self.db.execute(select(Workflow).where(Workflow.id == id_, Workflow.organization_id == org_id)
                               ).unique().scalar_one_or_none()

    def list(self, org_id: int):
        return self.db.execute(select(Workflow).where(Workflow.organization_id == org_id).order_by(Workflow.id)
                               ).unique().scalars().all()

    def active_list(self, org_id: int):
        return self.db.execute(select(Workflow).where(
            Workflow.organization_id == org_id, Workflow.is_active.is_(True))).unique().scalars().all()

    def by_name(self, org_id: int, name: str):
        return self.db.execute(select(Workflow).where(
            Workflow.organization_id == org_id, func.lower(Workflow.name) == name.lower())
        ).unique().scalar_one_or_none()


class ReviewRepo(BaseRepo):
    model = DocumentReview

    def pending_for_user(self, ctx: Ctx, show_all: bool, page: int, page_size: int):
        stmt = select(DocumentReview).join(Document, Document.id == DocumentReview.document_id).where(
            Document.organization_id == ctx.org_id, DocumentReview.status == ReviewStatus.PENDING.value)
        if not show_all:
            stmt = stmt.where(DocumentReview.reviewer_id == ctx.user.id)
        return paginate(self.db, stmt.order_by(DocumentReview.created_at, DocumentReview.id), page, page_size)

    def for_document_stage(self, document_id: int, submission_no: int, stage_order: int):
        return self.db.execute(select(DocumentReview).where(
            DocumentReview.document_id == document_id, DocumentReview.submission_no == submission_no,
            DocumentReview.stage_order == stage_order)).scalars().all()

    def pending_for_document(self, document_id: int):
        return self.db.execute(select(DocumentReview).where(
            DocumentReview.document_id == document_id, DocumentReview.status == ReviewStatus.PENDING.value)
        ).scalars().all()

    def pending_for_reviewer_in_org(self, org_id: int, reviewer_id: int):
        return self.db.execute(select(DocumentReview).join(Document, Document.id == DocumentReview.document_id).where(
            Document.organization_id == org_id, DocumentReview.reviewer_id == reviewer_id,
            DocumentReview.status == ReviewStatus.PENDING.value)).scalars().all()

    def overdue(self, now: datetime):
        return self.db.execute(select(DocumentReview).where(
            DocumentReview.status == ReviewStatus.PENDING.value, DocumentReview.due_at.is_not(None),
            DocumentReview.due_at < now, DocumentReview.escalated_at.is_(None))).scalars().all()

    def needing_reminder(self, before: datetime):
        return self.db.execute(select(DocumentReview).where(
            DocumentReview.status == ReviewStatus.PENDING.value,
            or_(and_(DocumentReview.last_reminded_at.is_(None), DocumentReview.created_at < before),
                DocumentReview.last_reminded_at < before))).scalars().all()

    def history(self, document_id: int):
        return self.db.execute(select(ApprovalHistory).where(ApprovalHistory.document_id == document_id)
                               .order_by(ApprovalHistory.id)).scalars().all()


class RetentionRepo(BaseRepo):
    model = RetentionPolicy

    def get_in_org(self, org_id: int, id_: int):
        return self.db.execute(select(RetentionPolicy).where(
            RetentionPolicy.id == id_, RetentionPolicy.organization_id == org_id)).scalar_one_or_none()

    def for_category(self, org_id: int, category_id: int, active_only=True):
        stmt = select(RetentionPolicy).where(RetentionPolicy.organization_id == org_id,
                                             RetentionPolicy.category_id == category_id)
        if active_only:
            stmt = stmt.where(RetentionPolicy.is_active.is_(True))
        return self.db.execute(stmt).scalar_one_or_none()

    def list(self, org_id: int):
        return self.db.execute(select(RetentionPolicy).where(RetentionPolicy.organization_id == org_id)
                               .order_by(RetentionPolicy.id)).scalars().all()


class NotificationRepo(BaseRepo):
    model = Notification

    def exists_dedupe(self, user_id: int, key: str) -> bool:
        return self.db.execute(select(Notification.id).where(
            Notification.user_id == user_id, Notification.dedupe_key == key)).first() is not None

    def list_for_user(self, user_id: int, unread_only: bool, page: int, page_size: int):
        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        return paginate(self.db, stmt.order_by(Notification.id.desc()), page, page_size)

    def unread_for_user(self, user_id: int):
        return self.db.execute(select(Notification).where(
            Notification.user_id == user_id, Notification.is_read.is_(False))).scalars().all()


class JobRepo(BaseRepo):
    model = BackgroundJob

    def active_by_key(self, key: str):
        return self.db.execute(select(BackgroundJob).where(
            BackgroundJob.dedupe_key == key,
            BackgroundJob.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]))).scalar_one_or_none()

    def due_ids(self, now: datetime, limit: int):
        return list(self.db.execute(select(BackgroundJob.id).where(
            BackgroundJob.status == JobStatus.PENDING.value, BackgroundJob.run_after <= now)
            .order_by(BackgroundJob.id).limit(limit)).scalars())

    def claim(self, job_id: int):
        """Row-locks the job so two workers cannot run it at once (SKIP LOCKED on PostgreSQL)."""
        return self.db.execute(select(BackgroundJob).where(
            BackgroundJob.id == job_id, BackgroundJob.status == JobStatus.PENDING.value)
            .with_for_update(skip_locked=True)).scalar_one_or_none()

    def list(self, status: str | None, page: int, page_size: int):
        stmt = select(BackgroundJob)
        if status:
            stmt = stmt.where(BackgroundJob.status == status)
        return paginate(self.db, stmt.order_by(BackgroundJob.id.desc()), page, page_size)


class AuditRepo(BaseRepo):
    model = AuditLog

    def search(self, org_id: int | None, *, action=None, entity_type=None, entity_id=None, actor_id=None,
               page=1, page_size=50):
        stmt = select(AuditLog)
        if org_id is not None:
            stmt = stmt.where(AuditLog.organization_id == org_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if actor_id:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        return paginate(self.db, stmt.order_by(AuditLog.id.desc()), page, page_size)
