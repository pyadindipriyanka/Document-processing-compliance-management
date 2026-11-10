import uuid

from app.core.config import get_settings
from app.core.errors import AppError, conflict, forbidden, unauthorized
from app.core.security import (burn_password_check, create_access_token, create_refresh_token, decode_token,
                               hash_password, verify_password)
from app.core.types import as_utc, utcnow
from app.models import RefreshToken, User
from app.repositories.repos import TokenRepo, UserRepo
from app.services import audit


class AuthService:
    def __init__(self, db):
        self.db, self.users, self.tokens = db, UserRepo(db), TokenRepo(db)

    def register(self, email: str, password: str, full_name: str) -> User:
        email = email.lower()
        if self.users.by_email(email):
            raise conflict("EMAIL_ALREADY_REGISTERED", "An account with this e-mail already exists.")
        s = get_settings()
        user = self.users.add(User(email=email, full_name=full_name.strip(), hashed_password=hash_password(password),
                                   is_super_admin=bool(s.SUPER_ADMIN_EMAIL and s.SUPER_ADMIN_EMAIL.lower() == email)))
        audit.record(self.db, action="USER_REGISTERED", entity_type="user", entity_id=user.id, actor_id=user.id,
                     meta={"email": email})
        self.db.commit()
        return user

    def _issue(self, user: User, family_id: str) -> dict:
        jti = uuid.uuid4().hex
        refresh, expires_at = create_refresh_token(user.id, family_id, jti)
        self.tokens.add(RefreshToken(user_id=user.id, jti=jti, family_id=family_id, expires_at=expires_at))
        access, ttl = create_access_token(user.id, family_id)
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer", "expires_in": ttl,
                "_jti": jti}

    def login(self, email: str, password: str) -> dict:
        user = self.users.by_email(email)
        if user is None:
            burn_password_check(password)
        if user is None or not verify_password(password, user.hashed_password):
            audit.record(self.db, action="LOGIN_FAILED", entity_type="user", entity_id=user.id if user else None,
                         actor_id=user.id if user else None, meta={"email": email.lower(), "reason": "bad_credentials"})
            self.db.commit()  # persist the failed attempt even though we raise
            raise unauthorized("INVALID_CREDENTIALS", "Invalid e-mail or password.")
        if not user.is_active:
            audit.record(self.db, action="LOGIN_FAILED", entity_type="user", entity_id=user.id, actor_id=user.id,
                         meta={"reason": "inactive"})
            self.db.commit()
            raise forbidden("ACCOUNT_INACTIVE", "This account is deactivated.")
        tokens = self._issue(user, uuid.uuid4().hex)
        audit.record(self.db, action="LOGIN", entity_type="user", entity_id=user.id, actor_id=user.id)
        self.db.commit()
        tokens.pop("_jti")
        return tokens

    def refresh(self, refresh_token: str) -> dict:
        payload = decode_token(refresh_token, "refresh")
        row = self.tokens.by_jti_for_update(payload["jti"])
        if row is None:
            raise unauthorized("INVALID_TOKEN", "Unknown refresh token.")
        if row.revoked_at is not None:
            # a rotated/revoked token came back: assume theft and kill the whole session family
            self.tokens.revoke_family(row.family_id)
            audit.record(self.db, action="REFRESH_TOKEN_REUSE", entity_type="user", entity_id=row.user_id,
                         actor_id=row.user_id)
            self.db.commit()
            raise unauthorized("REFRESH_TOKEN_REUSED", "Refresh token was already used; session revoked.")
        if as_utc(row.expires_at) <= utcnow():
            raise unauthorized("TOKEN_EXPIRED", "Refresh token expired.")
        user = self.users.get(row.user_id)
        if not user or not user.is_active:
            raise unauthorized("ACCOUNT_INACTIVE", "This account is deactivated.")
        row.revoked_at = utcnow()
        tokens = self._issue(user, row.family_id)
        row.replaced_by_jti = tokens.pop("_jti")
        self.db.commit()
        return tokens

    def logout(self, session_id: str, user: User) -> None:
        self.tokens.revoke_family(session_id)
        audit.record(self.db, action="LOGOUT", entity_type="user", entity_id=user.id, actor_id=user.id)
        self.db.commit()

    def change_password(self, user: User, current: str, new: str) -> None:
        if not verify_password(current, user.hashed_password):
            raise AppError(400, "INVALID_PASSWORD", "Current password is incorrect.")
        if verify_password(new, user.hashed_password):
            raise AppError(400, "PASSWORD_UNCHANGED", "New password must differ from the current one.")
        user.hashed_password = hash_password(new)
        self.tokens.revoke_all_for_user(user.id)  # every session must log in again
        audit.record(self.db, action="PASSWORD_CHANGED", entity_type="user", entity_id=user.id, actor_id=user.id)
        self.db.commit()
