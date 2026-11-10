from dataclasses import dataclass

from app.core.enums import Role
from app.models import User


@dataclass
class Ctx:
    """Authenticated request context: who is acting, inside which organisation, in which role."""
    user: User
    org_id: int
    role: str
    is_super: bool = False

    @property
    def is_admin(self) -> bool:
        return self.is_super or self.role == Role.ORG_ADMIN

    @property
    def is_manager(self) -> bool:
        return self.is_admin or self.role == Role.DOCUMENT_MANAGER

    @property
    def is_read_only(self) -> bool:
        return not self.is_super and self.role == Role.AUDITOR
