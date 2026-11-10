"""Database-backed background job runner (FastAPI BackgroundTasks + optional scheduler loop).

* Jobs are rows, so a worker crash never loses work or half-applies a workflow transition:
  workflow operations commit synchronously, jobs only do secondary work (indexing, scans, e-mail ...).
* `dedupe_key` + a partial unique index (PENDING/RUNNING) prevent duplicate processing.
* Failures are retried with exponential back-off up to `max_attempts`; the last error is recorded.
Swap `drain` for a Celery task calling `run_job` if a broker is available.
"""
import logging
import re
import smtplib
from datetime import date, timedelta
from email.message import EmailMessage

from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.enums import DocStatus, JobStatus, JobType, NotificationType, ReviewStatus, Role
from app.core.types import utcnow
from app.models import BackgroundJob
from app.repositories.repos import (DocumentRepo, JobRepo, MemberRepo, NotificationRepo, RetentionRepo, ReviewRepo,
                                    VersionRepo)
from app.services import audit
from app.services.notifications import notify
from app.services.transitions import transition

log = logging.getLogger("app.jobs")


class PermanentJobError(Exception):
    """Failures that must not be retried. `audit_entry` is recorded after the job's work is rolled back."""

    def __init__(self, message: str, audit_entry: dict | None = None):
        super().__init__(message)
        self.audit_entry = audit_entry


def enqueue(db, job_type, dedupe_key: str | None = None, payload: dict | None = None, delay_seconds: int = 0):
    repo = JobRepo(db)
    if dedupe_key:
        existing = repo.active_by_key(dedupe_key)
        if existing:
            return existing
    job = BackgroundJob(job_type=str(job_type), dedupe_key=dedupe_key, payload=payload or {},
                        max_attempts=get_settings().JOB_MAX_ATTEMPTS,
                        run_after=utcnow() + timedelta(seconds=delay_seconds))
    try:
        with db.begin_nested():
            repo.add(job)
    except IntegrityError:  # lost a race with another enqueuer
        return repo.active_by_key(dedupe_key)
    return job


# ----------------------------------------------------------------------------- handlers
def _checksum_verify(db, payload):
    from app.models import DocumentFile
    from app.storage.local import LocalStorage
    import hashlib
    f = db.get(DocumentFile, payload["file_id"])
    if f is None:
        raise PermanentJobError("file not found")
    path = LocalStorage().path_for(f.storage_key)
    sha = hashlib.sha256()
    with open(path, "rb") as fh:  # OSError here is transient -> retried
        while chunk := fh.read(1024 * 1024):
            sha.update(chunk)
    if sha.hexdigest() != f.checksum_sha256:
        raise PermanentJobError(
            "checksum mismatch: stored file differs from recorded checksum",
            {"action": "CHECKSUM_MISMATCH", "entity_type": "file", "entity_id": f.id, "org_id": f.organization_id,
             "meta": {"expected": f.checksum_sha256, "actual": sha.hexdigest()}})
    return {"verified": True, "sha256": f.checksum_sha256}


_PATTERNS = {
    "contract_number": re.compile(r"contract\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I),
    "vendor": re.compile(r"vendor\s*[:\-]\s*([^\n]{2,80})", re.I),
    "issue_date": re.compile(r"issue(?:d)?\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})", re.I),
    "expiration_date": re.compile(r"expir(?:y|ation)\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})", re.I),
}


def _metadata_extraction(db, payload):
    """Suggestions only: they are stored separately and never applied without manual confirmation."""
    from app.models import DocumentMetadata
    from app.storage.local import LocalStorage
    version = VersionRepo(db).get(payload["version_id"])
    if version is None:
        raise PermanentJobError("version not found")
    if version.file.content_type != "application/pdf":
        return {"skipped": "not a PDF"}
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"skipped": "pypdf not installed"}
    try:
        reader = PdfReader(str(LocalStorage().path_for(version.file.storage_key)))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    except OSError:
        raise  # storage problem: transient, retried
    except Exception as exc:  # malformed / encrypted PDF: nothing to suggest, retrying cannot help
        return {"skipped": f"unreadable PDF ({type(exc).__name__})"}
    found = {k: m.group(1).strip() for k, p in _PATTERNS.items() if (m := p.search(text))}
    row = DocumentRepo(db).metadata_row(version.document_id)
    if row is None:
        row = DocumentMetadata(document_id=version.document_id, data={})
        db.add(row)
    row.suggested = {"version": version.version_number, "values": found}
    return {"suggested": found}


def _document_indexing(db, payload):
    from app.services.documents import DocumentService
    repo, svc = DocumentRepo(db), DocumentService(db)
    ids = [payload["document_id"]] if payload.get("document_id") else repo.all_ids()
    for i in ids:
        doc = repo.get(i)
        if doc is not None:
            svc.reindex(doc)
    return {"indexed": len(ids)}


def _admins(db, org_id):
    return [m.user_id for m in MemberRepo(db).active_users_with_roles(org_id, [Role.ORG_ADMIN.value])]


def _expiration_scan(db, payload):
    repo, today = DocumentRepo(db), date.today()
    expired = soon = 0
    for doc in repo.approved_past_expiry(today):
        transition(doc, DocStatus.EXPIRED)
        expired += 1
        audit.record(db, action="DOCUMENT_EXPIRED", entity_type="document", entity_id=doc.id, org_id=doc.organization_id,
                     meta={"expiration_date": doc.expiration_date})
        for uid in {doc.owner_id, *_admins(db, doc.organization_id)}:
            notify(db, user_id=uid, org_id=doc.organization_id, type_=NotificationType.DOCUMENT_EXPIRED,
                   title=f"Expired: {doc.title}", message=f"Expired on {doc.expiration_date}.",
                   entity_type="document", entity_id=doc.id, dedupe_key=f"expired:{doc.id}:{doc.expiration_date}:{uid}")
    s = get_settings()
    horizon = repo.approved_expiring_between(today, today + timedelta(days=365))
    for doc in horizon:
        policy = RetentionRepo(db).for_category(doc.organization_id, doc.category_id)
        window = policy.notify_before_days if policy else s.EXPIRY_NOTICE_DAYS
        days_left = (doc.expiration_date - today).days
        if days_left <= window:
            soon += 1
            for uid in {doc.owner_id, *_admins(db, doc.organization_id)}:
                notify(db, user_id=uid, org_id=doc.organization_id, type_=NotificationType.DOCUMENT_EXPIRING,
                       title=f"Expiring soon: {doc.title}", message=f"Expires in {days_left} day(s).",
                       entity_type="document", entity_id=doc.id,
                       dedupe_key=f"expiring:{doc.id}:{doc.expiration_date}:{uid}")
    return {"expired": expired, "expiring_notified": soon}


def _retention_check(db, payload):
    """EXPIRED documents whose category policy says auto_archive_expired move to ARCHIVED (still readable,
    restorable, and protected from deletion while the retention period runs)."""
    archived = 0
    for doc in DocumentRepo(db).expired_docs():
        policy = RetentionRepo(db).for_category(doc.organization_id, doc.category_id)
        if policy and policy.auto_archive_expired:
            doc.previous_status = doc.status
            transition(doc, DocStatus.ARCHIVED)
            archived += 1
            audit.record(db, action="DOCUMENT_ARCHIVED", entity_type="document", entity_id=doc.id,
                         org_id=doc.organization_id, meta={"reason": "retention_policy_auto_archive"})
    return {"archived": archived}


def _approval_reminders(db, payload):
    s, now, sent = get_settings(), utcnow(), 0
    for r in ReviewRepo(db).needing_reminder(utcnow() - timedelta(hours=s.REVIEW_REMINDER_HOURS)):
        doc = DocumentRepo(db).get(r.document_id)
        r.last_reminded_at = now
        sent += 1
        notify(db, user_id=r.reviewer_id, org_id=doc.organization_id, type_=NotificationType.REVIEW_REMINDER,
               title=f"Reminder: {doc.title}", message="A review is waiting for your decision.",
               entity_type="review", entity_id=r.id, dedupe_key=f"reminder:{r.id}:{now:%Y%m%d%H}")
    return {"reminders": sent}


def _reviewer_escalation(db, payload):
    escalated = 0
    for r in ReviewRepo(db).overdue(utcnow()):
        doc = DocumentRepo(db).get(r.document_id)
        stage = next((s for s in (doc.workflow_snapshot or {}).get("stages", []) if s["order"] == r.stage_order), {})
        targets = {stage["escalate_to_user_id"]} if stage.get("escalate_to_user_id") else set(_admins(db, doc.organization_id))
        r.escalated_at = utcnow()
        escalated += 1
        for uid in targets | {r.reviewer_id}:
            notify(db, user_id=uid, org_id=doc.organization_id, type_=NotificationType.REVIEWER_ESCALATION,
                   title=f"Overdue review: {doc.title}", message=f"Stage '{r.stage_name}' is past its due time.",
                   entity_type="review", entity_id=r.id, dedupe_key=f"escalation:{r.id}:{uid}")
        audit.record(db, action="REVIEW_ESCALATED", entity_type="review", entity_id=r.id, org_id=doc.organization_id,
                     meta={"document_id": doc.id, "notified": sorted(targets)})
    return {"escalated": escalated}


def _notification_delivery(db, payload):
    from app.models import User
    n = NotificationRepo(db).get(payload["notification_id"])
    if n is None or n.emailed_at is not None:
        return {"skipped": True}
    s = get_settings()
    if not s.EMAIL_ENABLED:
        return {"skipped": "email disabled"}
    user = db.get(User, n.user_id)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = s.EMAIL_FROM, user.email, n.title
    msg.set_content(n.message)
    with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=10) as smtp:  # smtplib errors are transient -> retried
        if s.SMTP_USER:
            smtp.login(s.SMTP_USER, s.SMTP_PASSWORD)
        smtp.send_message(msg)
    n.emailed_at = utcnow()
    return {"sent_to": user.email}


HANDLERS = {
    JobType.CHECKSUM_VERIFY: _checksum_verify, JobType.METADATA_EXTRACTION: _metadata_extraction,
    JobType.DOCUMENT_INDEXING: _document_indexing, JobType.EXPIRATION_SCAN: _expiration_scan,
    JobType.RETENTION_CHECK: _retention_check, JobType.APPROVAL_REMINDERS: _approval_reminders,
    JobType.REVIEWER_ESCALATION: _reviewer_escalation, JobType.NOTIFICATION_DELIVERY: _notification_delivery,
}


# ----------------------------------------------------------------------------- runner
def run_job(db, job_id: int) -> str | None:
    """Claims and runs one job. Returns the final status, or None if another worker already took it."""
    repo = JobRepo(db)
    job = repo.claim(job_id)
    if job is None:
        db.rollback()
        return None
    job.status, job.started_at, job.attempts = JobStatus.RUNNING.value, utcnow(), job.attempts + 1
    db.commit()  # visible as RUNNING; the dedupe index now blocks duplicates
    try:
        with db.begin_nested():
            result = HANDLERS[JobType(job.job_type)](db, job.payload or {})
        job.status, job.result, job.last_error = JobStatus.SUCCEEDED.value, result, None
        job.finished_at = utcnow()
    except PermanentJobError as exc:
        db.rollback()
        job = repo.get(job_id)
        job.status, job.last_error, job.finished_at = JobStatus.FAILED.value, str(exc), utcnow()
        if exc.audit_entry:
            audit.record(db, **exc.audit_entry)
    except Exception as exc:  # transient: retry with exponential back-off
        db.rollback()
        job = repo.get(job_id)
        job.last_error = f"{type(exc).__name__}: {exc}"
        if job.attempts >= job.max_attempts:
            job.status, job.finished_at = JobStatus.FAILED.value, utcnow()
        else:
            job.status = JobStatus.PENDING.value
            job.run_after = utcnow() + timedelta(seconds=min(2 ** job.attempts * 30, 3600))
        log.warning("job %s failed (attempt %s): %s", job.id, job.attempts, exc)
    db.commit()
    return job.status


def drain(db, limit: int = 50) -> int:
    ran = 0
    for job_id in JobRepo(db).due_ids(utcnow(), limit):
        if run_job(db, job_id) is not None:
            ran += 1
    return ran


def drain_in_new_session() -> None:
    from app.db import session as session_module
    db = session_module.SessionLocal()
    try:
        drain(db)
    except Exception:  # background work must never break the request lifecycle
        log.exception("job drain failed")
    finally:
        db.close()


def run_scheduled(db) -> list[int]:
    """Enqueues the periodic jobs (one per hour per type, deduplicated)."""
    stamp = utcnow().strftime("%Y%m%d%H")
    ids = []
    for jt in (JobType.EXPIRATION_SCAN, JobType.RETENTION_CHECK, JobType.APPROVAL_REMINDERS,
               JobType.REVIEWER_ESCALATION):
        job = enqueue(db, jt, f"scheduled:{jt.value}:{stamp}", {})
        ids.append(job.id)
    db.commit()
    return ids
