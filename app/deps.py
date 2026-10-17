import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import Role, User
from .security import decode_access_token

DB = Annotated[Session, Depends(get_db)]

_bearer = HTTPBearer(auto_error=False)
_UNAUTHORIZED = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in",
                              headers={"WWW-Authenticate": "Bearer"})


def optional_user(db: DB,
                  creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User | None:
    if creds is None:
        return None
    try:
        claims = decode_access_token(creds.credentials)
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        raise _UNAUTHORIZED
    user = db.get(User, user_id)
    if user is None:  # account deleted after the token was issued
        raise _UNAUTHORIZED
    return user


def current_user(user: Annotated[User | None, Depends(optional_user)]) -> User:
    if user is None:
        raise _UNAUTHORIZED
    return user


def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    # Role is read from the database, not the token, so a demotion takes effect immediately.
    if user.role != Role.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


CurrentUser = Annotated[User, Depends(current_user)]
OptionalUser = Annotated[User | None, Depends(optional_user)]
AdminUser = Annotated[User, Depends(admin_user)]
