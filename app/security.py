"""Password hashing and tokens.

Access token: a signed JWT that lives 15 minutes, sent as "Authorization: Bearer ...".
Refresh token: a random string that lives 30 days in an HttpOnly cookie. Only its
SHA-256 is stored, so a leaked database can't be used to sign in.
CC-203: the access token carries the account's session version ("sv"). Raising the version
ends every access token at once.
"""
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from .config import get_settings

ALGORITHM = "HS256"

# Hash a fixed string once so a login for an unknown email takes as long as one for a
# known email (otherwise response timing reveals which emails have accounts).
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


BCRYPT_MAX_BYTES = 72


def verify_password(password: str, password_hash: str | None) -> bool:
    raw = password.encode()
    # CC-191: bcrypt refuses anything over 72 bytes, and no password we store is that long (sign-up
    # never allowed it), so a longer one is simply wrong. Hash the dummy anyway so it takes as long.
    if password_hash is None or len(raw) > BCRYPT_MAX_BYTES:
        bcrypt.checkpw(raw[:BCRYPT_MAX_BYTES], _DUMMY_HASH.encode())
        return False
    return bcrypt.checkpw(raw, password_hash.encode())


def create_access_token(user_id: uuid.UUID, role: str, session_version: int = 0) -> tuple[str, int]:
    s = get_settings()
    now = datetime.now(UTC)
    expires_in = s.access_token_minutes * 60
    claims = {"sub": str(user_id), "role": role, "sv": session_version, "iss": s.jwt_issuer,
              "iat": now, "exp": now + timedelta(seconds=expires_in)}
    return jwt.encode(claims, s.jwt_secret, algorithm=ALGORITHM), expires_in


def decode_access_token(token: str) -> dict:
    """Raises jwt.PyJWTError if the token is forged, expired or from someone else."""
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[ALGORITHM], issuer=s.jwt_issuer,
                      options={"require": ["sub", "exp", "iss"]})


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
