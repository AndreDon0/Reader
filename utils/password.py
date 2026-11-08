import logging

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError

hasher = PasswordHasher()
logger = logging.getLogger(__name__)


def hash_password(password: str) -> str | None:
    """Return a hash, or None on failure; callers must reject a missing hash."""
    if not isinstance(password, (str, bytes)):
        logger.error("Password hashing failed: invalid input type")
        return None
    try:
        return hasher.hash(password)
    except (HashingError, TypeError):
        logger.error("Password hashing failed")
        return None


def verify_password(stored_hash: str, password: str) -> bool:
    if not isinstance(stored_hash, (str, bytes)) or not isinstance(password, (str, bytes)):
        logger.error("Password verification failed: invalid input type")
        return False
    try:
        return hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        logger.debug("Password verification did not match")
        return False
    except (InvalidHashError, VerificationError, TypeError):
        logger.error("Password verification failed: invalid hash or input")
        return False
