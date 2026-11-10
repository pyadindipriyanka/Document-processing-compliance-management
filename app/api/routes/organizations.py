from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import User
from app.schemas.schemas import (MemberIn, MemberOut, MemberPatch, OrganizationIn, OrganizationOut,
                                 OrganizationPatch)
from app.services.organizations import OrganizationService

router = APIRouter(prefix="/organizations", tags=["Organizations"])


def _member(m) -> MemberOut:
    return MemberOut(id=m.id, organization_id=m.organization_id, user_id=m.user_id, role=m.role,
                     is_active=m.is_active, email=m.user.email, full_name=m.user.full_name)


@router.post("", response_model=OrganizationOut, status_code=201)
def create_org(body: OrganizationIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return OrganizationService(db).create(user, body.name, body.description)


@router.get("", response_model=list[OrganizationOut])
def list_orgs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return OrganizationService(db).orgs.list_for(user)


@router.get("/{organization_id}", response_model=OrganizationOut)
def get_org(organization_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return OrganizationService(db).require_member(user, organization_id)[0]


@router.patch("/{organization_id}", response_model=OrganizationOut)
def update_org(organization_id: int, body: OrganizationPatch, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    return OrganizationService(db).update(user, organization_id, body.model_dump(exclude_unset=True))


@router.get("/{organization_id}/members", response_model=list[MemberOut])
def list_members(organization_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [_member(m) for m in OrganizationService(db).list_members(user, organization_id)]


@router.post("/{organization_id}/members", response_model=MemberOut, status_code=201)
def add_member(organization_id: int, body: MemberIn, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    return _member(OrganizationService(db).add_member(user, organization_id, body.email, body.user_id, body.role))


@router.patch("/{organization_id}/members/{member_id}", response_model=MemberOut)
def update_member(organization_id: int, member_id: int, body: MemberPatch, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    return _member(OrganizationService(db).update_member(user, organization_id, member_id, body.role, body.is_active))


@router.delete("/{organization_id}/members/{member_id}", response_model=MemberOut)
def remove_member(organization_id: int, member_id: int, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    """Soft removal (deactivation) so document ownership and audit history are preserved."""
    return _member(OrganizationService(db).remove_member(user, organization_id, member_id))
