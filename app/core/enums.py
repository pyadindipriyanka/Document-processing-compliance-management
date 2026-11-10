from enum import StrEnum


class Role(StrEnum):
    SUPER_ADMIN = "super_admin"  # platform-level flag on the user, never stored on memberships
    ORG_ADMIN = "org_admin"
    DOCUMENT_MANAGER = "document_manager"
    REVIEWER = "reviewer"
    AUDITOR = "auditor"
    EMPLOYEE = "employee"


MEMBER_ROLES = [r for r in Role if r != Role.SUPER_ADMIN]


class DocStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"
    EXPIRED = "EXPIRED"


class AccessLevel(StrEnum):
    ORGANIZATION = "ORGANIZATION"  # every member of the organisation
    RESTRICTED = "RESTRICTED"  # owner, admins, managers, auditors, grants, assigned reviewers
    PRIVATE = "PRIVATE"  # owner, admins, grants, assigned reviewers


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    CANCELLED = "CANCELLED"
    REASSIGNED = "REASSIGNED"


class HistoryDecision(StrEnum):
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    REASSIGNED = "REASSIGNED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    FINAL_APPROVED = "FINAL_APPROVED"
    INVALIDATED = "INVALIDATED"
    RENEWED = "RENEWED"


class RejectionPolicy(StrEnum):
    ANY_REJECTION = "ANY_REJECTION"  # one rejection rejects the document
    STAGE_QUORUM = "STAGE_QUORUM"  # reject only when the stage can no longer reach its quorum


class FieldType(StrEnum):
    STRING = "string"
    DATE = "date"
    NUMBER = "number"
    BOOLEAN = "boolean"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class JobType(StrEnum):
    CHECKSUM_VERIFY = "CHECKSUM_VERIFY"
    METADATA_EXTRACTION = "METADATA_EXTRACTION"
    DOCUMENT_INDEXING = "DOCUMENT_INDEXING"
    EXPIRATION_SCAN = "EXPIRATION_SCAN"
    APPROVAL_REMINDERS = "APPROVAL_REMINDERS"
    REVIEWER_ESCALATION = "REVIEWER_ESCALATION"
    RETENTION_CHECK = "RETENTION_CHECK"
    NOTIFICATION_DELIVERY = "NOTIFICATION_DELIVERY"


class NotificationType(StrEnum):
    DOCUMENT_SUBMITTED = "DOCUMENT_SUBMITTED"
    REVIEW_ASSIGNED = "REVIEW_ASSIGNED"
    APPROVAL_COMPLETED = "APPROVAL_COMPLETED"
    DOCUMENT_REJECTED = "DOCUMENT_REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    DOCUMENT_EXPIRING = "DOCUMENT_EXPIRING"
    DOCUMENT_EXPIRED = "DOCUMENT_EXPIRED"
    REVIEWER_ESCALATION = "REVIEWER_ESCALATION"
    REVIEW_REMINDER = "REVIEW_REMINDER"
    VERSION_UPLOADED = "VERSION_UPLOADED"
    REVIEW_NEEDS_REASSIGNMENT = "REVIEW_NEEDS_REASSIGNMENT"
