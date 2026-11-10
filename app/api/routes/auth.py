from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import Auth, get_auth
from app.db.session import get_db
from app.repositories.repos import MemberRepo
from app.schemas.schemas import (ChangePasswordIn, LoginIn, MeOut, MembershipOut, RefreshIn, RegisterIn, TokenOut,
                                 UserOut)
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    return AuthService(db).register(body.email, body.password, body.full_name)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    return AuthService(db).login(body.email, body.password)


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    return AuthService(db).refresh(body.refresh_token)


@router.post("/logout")
def logout(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    AuthService(db).logout(auth.session_id, auth.user)
    return {"success": True}


@router.get("/me", response_model=MeOut)
def me(auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    data = UserOut.model_validate(auth.user).model_dump()
    members = [MembershipOut.model_validate(m) for m in MemberRepo(db).active_for_user(auth.user.id)]
    return MeOut(**data, memberships=members)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, auth: Auth = Depends(get_auth), db: Session = Depends(get_db)):
    AuthService(db).change_password(auth.user, body.current_password, body.new_password)
    return {"success": True}
