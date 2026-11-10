from dataclasses import dataclass
from typing import Optional

from fastapi import BackgroundTasks, Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.errors import bad_request, forbidden, unauthorized
from app.core.security import decode_token
from app.db.session import get_db
from app.models import User
from app.repositories.repos import MemberRepo, OrgRepo, TokenRepo, UserRepo
from app.services.access import Ctx

bearer = HTTPBearer(auto_error=False)


@dataclass
class Auth:
    user: User
    session_id: str


def get_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer), db: Session = Depends(get_db)) -> Auth:
    if creds is None:
        raise unauthorized()
    payload = decode_token(creds.credentials, "access")
    user = UserRepo(db).get(int(payload["sub"]))
    if user is None or not user.is_active:
        raise unauthorized("ACCOUNT_INACTIVE", "Account is inactive or missing.")
    if not TokenRepo(db).session_active(payload["sid"]):
        raise unauthorized("TOKEN_REVOKED", "Session has been revoked.")
    return Auth(user=user, session_id=payload["sid"])


def get_current_user(auth: Auth = Depends(get_auth)) -> User:
    return auth.user


def get_ctx(user: User = Depends(get_current_user), db: Session = Depends(get_db),
            x_organization_id: Optional[int] = Header(default=None)) -> Ctx:
    """Resolves the organisation the request acts in. Users with several memberships must send X-Organization-ID."""
    members = MemberRepo(db).active_for_user(user.id)
    if x_organization_id is not None:
        org = OrgRepo(db).get(x_organization_id)
        if user.is_super_admin:
            if org is None:
                raise forbidden("ORGANIZATION_ACCESS_DENIED", "Organization not accessible.")
            return Ctx(user=user, org_id=org.id, role="org_admin", is_super=True)
        member = next((m for m in members if m.organization_id == x_organization_id), None)
        if member is None or org is None or not org.is_active:
            raise forbidden("ORGANIZATION_ACCESS_DENIED", "You are not an active member of this organization.")
        return Ctx(user=user, org_id=member.organization_id, role=member.role)
    if user.is_super_admin:
        raise bad_request("ORGANIZATION_REQUIRED", "Super admins must send the X-Organization-ID header.")
    if not members:
        raise forbidden("NO_ORGANIZATION", "You do not belong to any active organization.")
    if len(members) > 1:
        raise bad_request("ORGANIZATION_REQUIRED", "You belong to several organizations; send X-Organization-ID.")
    org = OrgRepo(db).get(members[0].organization_id)
    if org is None or not org.is_active:
        raise forbidden("ORGANIZATION_ACCESS_DENIED", "Organization is inactive.")
    return Ctx(user=user, org_id=members[0].organization_id, role=members[0].role)


def require_roles(*roles: str):
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        if ctx.is_super or ctx.role in roles:
            return ctx
        raise forbidden("INSUFFICIENT_ROLE", "Your role is not permitted to perform this action.")
    return dep


def drain_jobs_after_response(background_tasks: BackgroundTasks) -> None:
    from app.services.jobs import drain_in_new_session
    background_tasks.add_task(drain_in_new_session)
