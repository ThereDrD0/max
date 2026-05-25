from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone

from app.domain import (
    AccessDeniedError,
    ConfigManagedOrganizerError,
    OrganizerAlreadyAssignedError,
    OrganizerRoleNotFoundError,
)
from app.storage.base import Storage
from app.storage.entities import RoleAssignment


class AdminService:
    def __init__(
        self,
        storage: Storage,
        *,
        organizer_config_user_ids: Iterable[int] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.organizer_config_user_ids = {
            int(user_id) for user_id in organizer_config_user_ids
        }
        self.now = now or (lambda: datetime.now(timezone.utc))

    def can_use_menu(
        self,
        actor_user_id: int,
        *,
        roles: set[str] | None = None,
    ) -> bool:
        roles = roles if roles is not None else self.storage.get_user_roles(actor_user_id)
        return "admin" in roles

    def add_organizer(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> RoleAssignment:
        self._require_admin(actor_user_id)
        if self.storage.has_role(target_user_id, "organizer"):
            raise OrganizerAlreadyAssignedError("Пользователь уже Организатор.")
        return self.storage.ensure_role(
            target_user_id,
            "organizer",
            created_at=self.now(),
            created_by_user_id=actor_user_id,
        )

    def remove_organizer(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> RoleAssignment:
        self._require_admin(actor_user_id)
        role = self.storage.get_role(target_user_id, "organizer")
        if role is None:
            raise OrganizerRoleNotFoundError("Пользователь не является Организатором.")
        if target_user_id in self.organizer_config_user_ids:
            raise ConfigManagedOrganizerError(
                "Этот Организатор задан через конфиг. Уберите его из ORGANIZER_USER_IDS."
            )
        self.storage.delete_role(target_user_id, "organizer")
        return role

    def list_organizers(self, actor_user_id: int) -> list[RoleAssignment]:
        self._require_admin(actor_user_id)
        return self.storage.list_roles("organizer")

    def get_organizer(
        self,
        actor_user_id: int,
        target_user_id: int,
    ) -> RoleAssignment:
        self._require_admin(actor_user_id)
        role = self.storage.get_role(target_user_id, "organizer")
        if role is None:
            raise OrganizerRoleNotFoundError("Пользователь не является Организатором.")
        return role

    def is_config_managed_organizer(self, user_id: int) -> bool:
        return user_id in self.organizer_config_user_ids

    def _require_admin(self, actor_user_id: int) -> None:
        if not self.can_use_menu(actor_user_id):
            raise AccessDeniedError("У вас нет доступа к меню администратора.")
