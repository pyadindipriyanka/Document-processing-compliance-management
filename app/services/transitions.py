from app.core.enums import DocStatus as S
from app.core.errors import conflict

TRANSITIONS: dict[str, set[str]] = {
    S.DRAFT: {S.SUBMITTED, S.ARCHIVED},
    S.SUBMITTED: {S.UNDER_REVIEW, S.DRAFT},
    S.UNDER_REVIEW: {S.APPROVED, S.REJECTED, S.DRAFT},
    S.APPROVED: {S.ARCHIVED, S.EXPIRED, S.DRAFT},
    S.REJECTED: {S.DRAFT, S.SUBMITTED, S.ARCHIVED},
    S.EXPIRED: {S.APPROVED, S.SUBMITTED, S.DRAFT, S.ARCHIVED},
    S.ARCHIVED: {S.DRAFT, S.APPROVED, S.REJECTED, S.EXPIRED},  # restore -> previous status only
}


def transition(doc, new_status) -> None:
    new_status = str(new_status)
    if new_status not in {str(s) for s in TRANSITIONS.get(doc.status, set())}:
        raise conflict("INVALID_WORKFLOW_TRANSITION",
                       f"The requested transition {doc.status} -> {new_status} is not permitted.")
    doc.status = new_status
