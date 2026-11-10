from app.core.config import get_settings
from app.core.enums import JobType
from app.core.errors import not_found
from app.models import Notification
from app.repositories.repos import NotificationRepo


def notify(db, *, user_id: int, org_id: int | None, type_: str, title: str, message: str,
           entity_type: str | None = None, entity_id: int | None = None, dedupe_key: str | None = None):
    repo = NotificationRepo(db)
    if dedupe_key and repo.exists_dedupe(user_id, dedupe_key):
        return None
    n = repo.add(Notification(user_id=user_id, organization_id=org_id, type=str(type_), title=title, message=message,
                              entity_type=entity_type, entity_id=entity_id, dedupe_key=dedupe_key))
    if get_settings().EMAIL_ENABLED:
        from app.services.jobs import enqueue
        enqueue(db, JobType.NOTIFICATION_DELIVERY, f"notify:{n.id}", {"notification_id": n.id})
    return n


class NotificationService:
    def __init__(self, db):
        self.db, self.repo = db, NotificationRepo(db)

    def list(self, user, unread_only, page, page_size):
        return self.repo.list_for_user(user.id, unread_only, page, page_size)

    def mark_read(self, user, notification_id: int):
        n = self.repo.get(notification_id)
        if not n or n.user_id != user.id:
            raise not_found("NOTIFICATION_NOT_FOUND", "Notification not found.")
        n.is_read = True
        self.db.commit()
        return n

    def mark_all_read(self, user) -> int:
        items = self.repo.unread_for_user(user.id)
        for n in items:
            n.is_read = True
        self.db.commit()
        return len(items)
