from app.core.enums import NotificationType, Role
from app.core.errors import bad_request, conflict, forbidden, not_found
from app.core.types import utcnow
from app.models import Organization, OrganizationMember
from app.repositories.repos import MemberRepo, OrgRepo, ReviewRepo, UserRepo
from app.services import audit
from app.services.notifications import notify


class OrganizationService:
    def __init__(self, db):
        self.db, self.orgs, self.members, self.users = db, OrgRepo(db), MemberRepo(db), UserRepo(db)

    # --- access helpers ---
    def _membership(self, user, org_id):
        return self.members.get_pair(org_id, user.id)

    def require_member(self, user, org_id: int):
        org = self.orgs.get(org_id)
        if org is None:
            raise not_found("ORGANIZATION_NOT_FOUND", "Organization not found.")
        if user.is_super_admin:
            return org, None
        m = self._membership(user, org_id)
        if m is None or not m.is_active:  # do not reveal other organisations
            raise not_found("ORGANIZATION_NOT_FOUND", "Organization not found.")
        return org, m

    def require_admin(self, user, org_id: int):
        org, m = self.require_member(user, org_id)
        if not user.is_super_admin and m.role != Role.ORG_ADMIN:
            raise forbidden("INSUFFICIENT_ROLE", "Organization admin role required.")
        return org

    # --- organisations ---
    def create(self, user, name: str, description: str | None):
        if self.orgs.by_name(name):
            raise conflict("ORGANIZATION_EXISTS", "An organization with this name already exists.")
        org = self.orgs.add(Organization(name=name.strip(), description=description, created_by=user.id))
        self.members.add(OrganizationMember(organization_id=org.id, user_id=user.id, role=Role.ORG_ADMIN.value))
        audit.record(self.db, action="ORGANIZATION_CREATED", entity_type="organization", entity_id=org.id,
                     actor_id=user.id, org_id=org.id, meta={"name": org.name})
        self.db.commit()
        return org

    def update(self, user, org_id: int, data: dict):
        org = self.require_admin(user, org_id)
        if "name" in data and data["name"] and data["name"].lower() != org.name.lower() and self.orgs.by_name(data["name"]):
            raise conflict("ORGANIZATION_EXISTS", "An organization with this name already exists.")
        before = {k: getattr(org, k) for k in data}
        for k, v in data.items():
            if v is not None:
                setattr(org, k, v)
        audit.record(self.db, action="ORGANIZATION_UPDATED", entity_type="organization", entity_id=org.id,
                     actor_id=user.id, org_id=org.id, meta={"before": before, "after": data})
        self.db.commit()
        return org

    # --- members ---
    def list_members(self, user, org_id: int):
        self.require_member(user, org_id)
        return self.members.list(org_id)

    def add_member(self, user, org_id: int, email, user_id, role: str):
        self.require_admin(user, org_id)
        target = self.users.by_email(email) if email else (self.users.get(user_id) if user_id else None)
        if target is None:
            raise not_found("USER_NOT_FOUND", "No user found for the supplied e-mail / id.")
        m = self.members.get_pair(org_id, target.id)
        if m is not None and m.is_active:
            raise conflict("MEMBER_EXISTS", "User is already an active member of this organization.")
        if m is None:
            m = self.members.add(OrganizationMember(organization_id=org_id, user_id=target.id, role=role))
        else:  # re-activation keeps the same row so ownership / audit references stay valid
            m.role, m.is_active, m.deactivated_at = role, True, None
        audit.record(self.db, action="MEMBER_ADDED", entity_type="organization_member", entity_id=m.id,
                     actor_id=user.id, org_id=org_id, meta={"user_id": target.id, "role": role})
        self.db.commit()
        return m

    def update_member(self, user, org_id: int, member_id: int, role, is_active):
        self.require_admin(user, org_id)
        m = self.members.get(member_id)
        if m is None or m.organization_id != org_id:
            raise not_found("MEMBER_NOT_FOUND", "Member not found.")
        losing_admin = m.is_active and m.role == Role.ORG_ADMIN and (
            (role is not None and role != Role.ORG_ADMIN) or is_active is False)
        if losing_admin and self.members.active_admin_count(org_id) <= 1:
            raise bad_request("LAST_ADMIN", "An organization must keep at least one active admin.")
        before = {"role": m.role, "is_active": m.is_active}
        if role is not None:
            m.role = role
        if is_active is not None:
            was_active, m.is_active = m.is_active, is_active
            m.deactivated_at = None if is_active else utcnow()
            if was_active and not is_active:
                self._flag_orphaned_reviews(org_id, m.user_id)
        audit.record(self.db, action="MEMBER_UPDATED", entity_type="organization_member", entity_id=m.id,
                     actor_id=user.id, org_id=org_id, meta={"before": before, "after": {"role": m.role, "is_active": m.is_active}})
        self.db.commit()
        return m

    def remove_member(self, user, org_id: int, member_id: int):
        """Soft removal: the membership row is deactivated, never deleted, so documents, reviews and
        audit rows that reference the user stay intact."""
        return self.update_member(user, org_id, member_id, None, False)

    def _flag_orphaned_reviews(self, org_id: int, user_id: int):
        pending = ReviewRepo(self.db).pending_for_reviewer_in_org(org_id, user_id)
        if not pending:
            return
        for admin in self.members.active_users_with_roles(org_id, [Role.ORG_ADMIN.value]):
            notify(self.db, user_id=admin.user_id, org_id=org_id, type_=NotificationType.REVIEW_NEEDS_REASSIGNMENT,
                   title="Reviews need reassignment",
                   message=f"A deactivated member still has {len(pending)} pending review(s).",
                   dedupe_key=f"orphaned:{user_id}:{len(pending)}:{admin.user_id}")
