"""SQLAlchemy models. Roles are an enum (app.core.enums.Role) stored on organization_members,
so there is no separate `roles` table; see README for the rationale."""
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, ForeignKey, ForeignKeyConstraint, Index,
                        Integer, String, Text, UniqueConstraint, event, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (AccessLevel, DocStatus, FieldType, HistoryDecision, JobStatus, MEMBER_ROLES,
                            RejectionPolicy, ReviewStatus)
from app.core.types import UTCDateTime, utcnow
from app.db.base import Base


def _in(column: str, enum) -> str:
    return f"{column} IN ({', '.join(repr(str(e.value)) for e in enum)})"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)


class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_member"),
        CheckConstraint(_in("role", MEMBER_ROLES), name="role_valid"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    user: Mapped[User] = relationship(lazy="joined", innerjoin=True)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    jti: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[str] = mapped_column(String(64), index=True)  # == session id ("sid" claim)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DocumentCategory(TimestampMixin, Base):
    __tablename__ = "document_categories"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_category_org_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    definitions: Mapped[list["MetadataDefinition"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="MetadataDefinition.id")


class MetadataDefinition(Base):
    __tablename__ = "metadata_definitions"
    __table_args__ = (
        UniqueConstraint("category_id", "key", name="uq_metadata_category_key"),
        CheckConstraint(_in("field_type", FieldType), name="field_type_valid"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("document_categories.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(120))
    field_type: Mapped[str] = mapped_column(String(16), default=FieldType.STRING.value)
    required: Mapped[bool] = mapped_column(Boolean, default=False)


class Document(TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in("status", DocStatus), name="status_valid"),
        CheckConstraint(_in("access_level", AccessLevel), name="access_level_valid"),
        CheckConstraint("current_version >= 0", name="current_version_nonneg"),
        Index("ix_documents_org_status", "organization_id", "status"),
        Index("ix_documents_org_category", "organization_id", "category_id"),
        Index("ix_documents_org_expiration", "organization_id", "expiration_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_number: Mapped[str] = mapped_column(String(40), unique=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int] = mapped_column(ForeignKey("document_categories.id", ondelete="RESTRICT"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default=DocStatus.DRAFT.value)
    previous_status: Mapped[str | None] = mapped_column(String(20))  # status to return to on restore
    access_level: Mapped[str] = mapped_column(String(20), default=AccessLevel.ORGANIZATION.value)
    expiration_date: Mapped[date | None] = mapped_column(Date)
    submission_no: Mapped[int] = mapped_column(Integer, default=0)
    workflow_id: Mapped[int | None] = mapped_column(ForeignKey("workflows.id", ondelete="SET NULL"))
    workflow_snapshot: Mapped[dict | None] = mapped_column(JSON)  # frozen config used by the in-flight approval
    current_stage: Mapped[int | None] = mapped_column(Integer)
    search_text: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    category: Mapped[DocumentCategory] = relationship(lazy="joined", innerjoin=True)


class DocumentAccess(Base):
    """Explicit per-user read grants (used by RESTRICTED / PRIVATE documents)."""
    __tablename__ = "document_access"
    __table_args__ = (UniqueConstraint("document_id", "user_id", name="uq_document_access"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class DocumentMetadata(Base):
    __tablename__ = "document_metadata"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested: Mapped[dict | None] = mapped_column(JSON)  # machine-extracted suggestions, never auto-applied
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class DocumentFile(Base):
    __tablename__ = "document_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    content_type: Mapped[str] = mapped_column(String(150))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_version_document_number"),
        CheckConstraint("version_number >= 1", name="version_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    file_id: Mapped[int] = mapped_column(ForeignKey("document_files.id", ondelete="RESTRICT"))
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    change_description: Mapped[str | None] = mapped_column(Text)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    is_material: Mapped[bool] = mapped_column(Boolean, default=True)
    restored_from_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    file: Mapped[DocumentFile] = relationship(lazy="joined", innerjoin=True)


class Workflow(TimestampMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_workflow_org_name"),
        CheckConstraint(_in("rejection_policy", RejectionPolicy), name="rejection_policy_valid"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    category_ids: Mapped[list] = mapped_column(JSON, default=list)
    rejection_policy: Mapped[str] = mapped_column(String(20), default=RejectionPolicy.ANY_REJECTION.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumped on every configuration change
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    stages: Mapped[list["WorkflowStage"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="WorkflowStage.stage_order")


class WorkflowStage(Base):
    __tablename__ = "workflow_stages"
    __table_args__ = (
        UniqueConstraint("workflow_id", "stage_order", name="uq_stage_workflow_order"),
        CheckConstraint("stage_order >= 1", name="order_positive"),
        CheckConstraint("required_approvals >= 1", name="required_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"), index=True)
    stage_order: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    required_approvals: Mapped[int] = mapped_column(Integer, default=1)
    require_all: Mapped[bool] = mapped_column(Boolean, default=False)
    escalate_after_hours: Mapped[int | None] = mapped_column(Integer)
    escalate_to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assignments: Mapped[list["WorkflowAssignment"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class WorkflowAssignment(Base):
    __tablename__ = "workflow_assignments"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND role IS NULL) OR (user_id IS NULL AND role IS NOT NULL)",
            name="user_xor_role"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("workflow_stages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str | None] = mapped_column(String(32))


class DocumentReview(Base):
    __tablename__ = "document_reviews"
    __table_args__ = (
        CheckConstraint(_in("status", ReviewStatus), name="status_valid"),
        Index("ix_reviews_reviewer_status", "reviewer_id", "status"),
        Index("ix_reviews_doc_submission_stage", "document_id", "submission_no", "stage_order"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="RESTRICT"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    submission_no: Mapped[int] = mapped_column(Integer)
    stage_order: Mapped[int] = mapped_column(Integer)
    stage_name: Mapped[str] = mapped_column(String(200))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(24), default=ReviewStatus.PENDING.value)
    comment: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApprovalHistory(Base):
    """Append-only. (document_id, version_number) is a composite FK to document_versions, so every
    decision is permanently tied to the exact version that was reviewed."""
    __tablename__ = "approval_history"
    __table_args__ = (
        ForeignKeyConstraint(["document_id", "version_number"],
                             ["document_versions.document_id", "document_versions.version_number"],
                             name="fk_history_version"),
        CheckConstraint(_in("decision", HistoryDecision), name="decision_valid"),
        Index("ix_history_document", "document_id", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(Integer)
    version_number: Mapped[int] = mapped_column(Integer)
    submission_no: Mapped[int] = mapped_column(Integer)
    stage_order: Mapped[int | None] = mapped_column(Integer)
    review_id: Mapped[int | None] = mapped_column(ForeignKey("document_reviews.id", ondelete="SET NULL"))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(24))
    comment: Mapped[str | None] = mapped_column(Text)
    metadata_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RetentionPolicy(TimestampMixin, Base):
    __tablename__ = "retention_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "category_id", name="uq_retention_org_category"),
        CheckConstraint("retention_days >= 0", name="retention_nonneg"),
        CheckConstraint("notify_before_days >= 0", name="notify_nonneg"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("document_categories.id", ondelete="CASCADE"))
    retention_days: Mapped[int] = mapped_column(Integer)
    prohibit_deletion: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_before_days: Mapped[int] = mapped_column(Integer, default=30)
    auto_archive_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_notification_dedupe"),
        Index("ix_notifications_user_read", "user_id", "is_read"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    emailed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JobStatus), name="status_valid"),
        # at most one PENDING/RUNNING job per dedupe key -> duplicate processing prevention
        Index("uq_jobs_active_dedupe", "dedupe_key", unique=True,
              postgresql_where=text("status IN ('PENDING','RUNNING')"),
              sqlite_where=text("status IN ('PENDING','RUNNING')")),
        Index("ix_jobs_status_run_after", "status", "run_after"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_org_created", "organization_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ImmutableRecordError(RuntimeError):
    pass


def _forbid(mapper, connection, target):
    raise ImmutableRecordError(f"{type(target).__name__} rows are append-only.")


for _model in (AuditLog, DocumentVersion, ApprovalHistory):
    event.listen(_model, "before_update", _forbid)
    event.listen(_model, "before_delete", _forbid)
