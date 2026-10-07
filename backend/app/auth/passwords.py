"""Password hashing and password policy (argon2id)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Argon2id with sensible defaults (64 MiB, 3 passes, 2 lanes).
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200  # refuses absurd inputs that would waste CPU


class PasswordPolicyError(ValueError):
    """Raised when a password does not meet the policy."""


def hash_password(password: str) -> str:
    """Hash a password. Never store the plaintext."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time verification that never raises on bad input."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


def validate_password_policy(password: str) -> list[str]:
    """Return a list of human-readable policy violations (empty means OK)."""
    problems: list[str] = []
    if not isinstance(password, str):
        return ["Password must be a string."]
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.")
    if not any(ch.isalpha() for ch in password):
        problems.append("Password must contain at least one letter.")
    if not any(ch.isdigit() for ch in password):
        problems.append("Password must contain at least one digit.")
    return problems
