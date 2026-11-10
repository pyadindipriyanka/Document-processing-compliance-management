from datetime import timedelta

from app.core.errors import conflict, not_found
from app.core.types import as_utc, utcnow
from app.models import RetentionPolicy
from app.repositories.repos import CategoryRepo, RetentionRepo
from app.services import audit


def retention_blocks_deletion(db, doc) -> bool:
    policy = RetentionRepo(db).for_category(doc.organization_id, doc.category_id)
    if not policy or not policy.prohibit_deletion or policy.retention_days <= 0:
        return False
    return utcnow() < as_utc(doc.created_at) + timedelta(days=policy.retention_days)


class RetentionService:
    def __init__(self, db):
        self.db, self.repo = db, RetentionRepo(db)

    def create(self, ctx, payload):
        if CategoryRepo(self.db).get_in_org(ctx.org_id, payload.category_id) is None:
            raise not_found("CATEGORY_NOT_FOUND", "Category not found.")
        if self.repo.for_category(ctx.org_id, payload.category_id, active_only=False):
            raise conflict("RETENTION_POLICY_EXISTS", "This category already has a retention policy; update it.")
        p = self.repo.add(RetentionPolicy(organization_id=ctx.org_id, **payload.model_dump()))
        audit.record(self.db, action="RETENTION_POLICY_CREATED", entity_type="retention_policy", entity_id=p.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta=payload.model_dump())
        self.db.commit()
        return p

    def update(self, ctx, policy_id: int, payload):
        p = self.repo.get_in_org(ctx.org_id, policy_id)
        if p is None:
            raise not_found("RETENTION_POLICY_NOT_FOUND", "Retention policy not found.")
        changes = payload.model_dump(exclude_none=True)
        before = {k: getattr(p, k) for k in changes}
        for k, v in changes.items():
            setattr(p, k, v)
        audit.record(self.db, action="RETENTION_POLICY_UPDATED", entity_type="retention_policy", entity_id=p.id,
                     actor_id=ctx.user.id, org_id=ctx.org_id, meta={"before": before, "after": changes})
        self.db.commit()
        return p

    def list(self, ctx):
        return self.repo.list(ctx.org_id)
