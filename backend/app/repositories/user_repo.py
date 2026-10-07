"""User data access."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..constants import Role
from ..models import User


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def get_by_id(session: Session, user_id: int) -> User | None:
    return session.get(User, user_id)


def get_by_email(session: Session, email: str) -> User | None:
    return session.execute(
        select(User).where(User.email == normalize_email(email))
    ).scalars().first()


def create_user(
    session: Session,
    email: str,
    password_hash: str,
    role: str = Role.USER,
) -> User:
    """Create a user. The role is always supplied by the server, never the client."""
    user = User(email=normalize_email(email), password_hash=password_hash, role=role)
    session.add(user)
    session.flush()
    return user


def count_users(session: Session) -> int:
    return int(session.execute(select(func.count(User.id))).scalar_one())
