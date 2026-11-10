import re
from datetime import date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.enums import AccessLevel, FieldType, MEMBER_ROLES, RejectionPolicy

T = TypeVar("T")
RoleName = str


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


# ---------- auth ----------
def _check_password(v: str) -> str:
    if len(v) < 8 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        raise ValueError("Password must be at least 8 characters and contain letters and digits.")
    return v


class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    full_name: str = Field(min_length=1, max_length=200)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        return _check_password(v)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v):
        return _check_password(v)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_super_admin: bool


class MembershipOut(ORM):
    organization_id: int
    role: str
    is_active: bool


class MeOut(UserOut):
    memberships: list[MembershipOut] = []


# ---------- organizations ----------
class OrganizationIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str | None = None


class OrganizationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = None
    is_active: bool | None = None


class OrganizationOut(ORM):
    id: int
    name: str
    description: str | None
    is_active: bool
    created_at: datetime


class MemberIn(BaseModel):
    email: EmailStr | None = None
    user_id: int | None = None
    role: str

    @field_validator("role")
    @classmethod
    def check_role(cls, v):
        if v not in MEMBER_ROLES:
            raise ValueError(f"role must be one of {[str(r) for r in MEMBER_ROLES]}")
        return v


class MemberPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def check_role(cls, v):
        if v is not None and v not in MEMBER_ROLES:
            raise ValueError(f"role must be one of {[str(r) for r in MEMBER_ROLES]}")
        return v


class MemberOut(ORM):
    id: int
    organization_id: int
    user_id: int
    role: str
    is_active: bool
    email: str | None = None
    full_name: str | None = None


# ---------- categories / metadata ----------
class MetadataDefinitionIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str | None = None
    field_type: FieldType = FieldType.STRING
    required: bool = False


class MetadataDefinitionOut(ORM):
    key: str
    label: str | None
    field_type: str
    required: bool


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None
    metadata_definitions: list[MetadataDefinitionIn] = []


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None
    metadata_definitions: list[MetadataDefinitionIn] | None = None  # replaces the whole list


class CategoryOut(ORM):
    id: int
    organization_id: int
    name: str
    description: str | None
    is_active: bool
    definitions: list[MetadataDefinitionOut] = []


# ---------- documents ----------
class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    category_id: int
    access_level: AccessLevel = AccessLevel.ORGANIZATION
    expiration_date: date | None = None
    allowed_user_ids: list[int] = []
    metadata: dict[str, Any] = {}


class DocumentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    access_level: AccessLevel | None = None
    expiration_date: date | None = None
    allowed_user_ids: list[int] | None = None


class DocumentOut(ORM):
    id: int
    document_number: str
    organization_id: int
    title: str
    description: str | None
    category_id: int
    owner_id: int
    current_version: int
    status: str
    access_level: str
    expiration_date: date | None
    submission_no: int
    created_at: datetime
    updated_at: datetime


class DocumentDetailOut(DocumentOut):
    metadata: dict[str, Any] = {}


class MetadataPatch(BaseModel):
    metadata: dict[str, Any]


class MetadataOut(BaseModel):
    data: dict[str, Any]
    suggested: dict[str, Any] | None = None


class SubmitIn(BaseModel):
    allow_expired: bool = False


class RenewIn(BaseModel):
    expiration_date: date
    comment: str | None = None


class FileOut(ORM):
    id: int
    document_id: int
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    uploaded_by: int | None
    created_at: datetime


class VersionOut(ORM):
    id: int
    document_id: int
    version_number: int
    file_id: int
    uploaded_by: int | None
    change_description: str | None
    checksum_sha256: str
    is_material: bool
    restored_from_version_id: int | None
    created_at: datetime


class RestoreVersionIn(BaseModel):
    change_description: str | None = None


# ---------- workflows ----------
class StageIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    required_approvals: int = Field(default=1, ge=1)
    require_all: bool = False
    escalate_after_hours: int | None = Field(default=None, ge=1)
    escalate_to_user_id: int | None = None
    reviewer_ids: list[int] = []
    reviewer_roles: list[str] = []


class WorkflowIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category_ids: list[int] = Field(min_length=1)
    rejection_policy: RejectionPolicy = RejectionPolicy.ANY_REJECTION
    stages: list[StageIn] = Field(min_length=1)  # list position == stage order


class WorkflowPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category_ids: list[int] | None = Field(default=None, min_length=1)
    rejection_policy: RejectionPolicy | None = None
    stages: list[StageIn] | None = Field(default=None, min_length=1)


class StageOut(BaseModel):
    stage_order: int
    name: str
    required_approvals: int
    require_all: bool
    escalate_after_hours: int | None
    escalate_to_user_id: int | None
    reviewer_ids: list[int]
    reviewer_roles: list[str]


class WorkflowOut(BaseModel):
    id: int
    organization_id: int
    name: str
    description: str | None
    category_ids: list[int]
    rejection_policy: str
    is_active: bool
    version: int
    stages: list[StageOut]


# ---------- reviews ----------
class ReviewOut(ORM):
    id: int
    document_id: int
    version_number: int
    submission_no: int
    stage_order: int
    stage_name: str
    reviewer_id: int
    status: str
    comment: str | None
    due_at: datetime | None
    escalated_at: datetime | None
    decided_at: datetime | None
    created_at: datetime


class DecisionIn(BaseModel):
    comment: str | None = None


class ReassignIn(BaseModel):
    new_reviewer_id: int
    reason: str | None = None


class ApprovalHistoryOut(ORM):
    id: int
    document_id: int
    version_number: int
    submission_no: int
    stage_order: int | None
    review_id: int | None
    actor_id: int | None
    decision: str
    comment: str | None
    metadata_snapshot: dict | None
    created_at: datetime


# ---------- retention ----------
class RetentionIn(BaseModel):
    category_id: int
    retention_days: int = Field(ge=0)
    prohibit_deletion: bool = True
    notify_before_days: int = Field(default=30, ge=0)
    auto_archive_expired: bool = False


class RetentionPatch(BaseModel):
    retention_days: int | None = Field(default=None, ge=0)
    prohibit_deletion: bool | None = None
    notify_before_days: int | None = Field(default=None, ge=0)
    auto_archive_expired: bool | None = None
    is_active: bool | None = None


class RetentionOut(ORM):
    id: int
    organization_id: int
    category_id: int
    retention_days: int
    prohibit_deletion: bool
    notify_before_days: int
    auto_archive_expired: bool
    is_active: bool


# ---------- notifications / audit / jobs ----------
class NotificationOut(ORM):
    id: int
    type: str
    title: str
    message: str
    entity_type: str | None
    entity_id: int | None
    is_read: bool
    created_at: datetime


class AuditLogOut(BaseModel):
    id: int
    actor_id: int | None
    organization_id: int | None
    action: str
    entity_type: str
    entity_id: str | None
    correlation_id: str | None
    metadata: dict | None
    created_at: datetime


class JobOut(ORM):
    id: int
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    result: dict | None
    created_at: datetime
    finished_at: datetime | None
