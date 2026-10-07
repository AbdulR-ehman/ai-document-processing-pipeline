"""Administrator bootstrap.

The admin role is never granted implicitly at registration time beyond the one
address named in ``ADMIN_EMAIL``. This module is how that account is actually
created (or promoted, or given a new password) - see ``scripts/seed_admin.py``
for the command line wrapper.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..auth.passwords import hash_password, validate_password_policy
from ..constants import AuditAction, Role
from ..models import User
from ..repositories import audit_repo, user_repo


class AdminSetupError(ValueError):
    """Raised when the administrator cannot be set up (safe message)."""


def ensure_admin(
    session: Session,
    email: str,
    password: str,
    *,
    request_id: str | None = None,
) -> tuple[User, str]:
    """Create or promote ``email`` to administrator.

    Returns ``(user, action)`` where action is ``created``, ``promoted``,
    ``password_updated`` or ``already_admin``. The password is validated against
    the normal policy and only ever stored as an argon2id hash.
    """
    normalized = user_repo.normalize_email(email)
    if not normalized or "@" not in normalized or "." not in normalized.split("@")[-1]:
        raise AdminSetupError("ADMIN_EMAIL is not a valid email address.")
    problems = validate_password_policy(password)
    if problems:
        raise AdminSetupError(" ".join(problems))

    user = user_repo.get_by_email(session, normalized)
    if user is None:
        user = user_repo.create_user(session, normalized, hash_password(password), Role.ADMIN)
        action = "created"
    elif user.role != Role.ADMIN:
        user.role = Role.ADMIN
        action = "promoted"
    elif not verify_current_password(user, password):
        user.password_hash = hash_password(password)
        action = "password_updated"
    else:
        action = "already_admin"

    audit_repo.log_action(
        session,
        actor_user_id=user.id,
        action=AuditAction.REGISTER,
        request_id=request_id,
        detail={"role": user.role, "seed": action},
    )
    session.commit()
    return user, action


def verify_current_password(user: User, password: str) -> bool:
    """True when ``password`` already matches the stored hash."""
    from ..auth.passwords import verify_password

    return verify_password(password, user.password_hash)