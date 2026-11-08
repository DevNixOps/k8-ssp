import hmac
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from .config import settings

ROLE_RANK = {"viewer": 1, "developer": 2, "admin": 3}
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


@dataclass
class User:
    name: str
    role: str
    namespaces: list[str]  # empty means all (admin) / unrestricted

    def can_access(self, ns: str) -> bool:
        return self.role == "admin" or not self.namespaces or ns in self.namespaces


def _load_users() -> dict[str, tuple[str, str, list[str]]]:
    users = {}
    for entry in filter(None, (e.strip() for e in settings.users.split(","))):
        parts = entry.split(":")
        name, pw, role = parts[0], parts[1], parts[2]
        nss = parts[3].split("|") if len(parts) > 3 else []
        users[name] = (pw, role, nss)
    return users


def authenticate(username: str, password: str) -> User | None:
    rec = _load_users().get(username)
    if rec and hmac.compare_digest(rec[0].encode(), password.encode()):
        return User(username, rec[1], rec[2])
    return None


def create_token(user: User) -> str:
    claims = {
        "sub": user.name,
        "role": user.role,
        "ns": user.namespaces,
        "exp": int(time.time()) + settings.token_ttl_minutes * 60,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> User:
    try:
        c = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return User(c["sub"], c["role"], c.get("ns", []))


def current_user(token: str = Depends(oauth2)) -> User:
    return decode_token(token)


def require_role(min_role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{min_role}'")
        return user

    return dep


def check_namespace(user: User, ns: str) -> None:
    if not user.can_access(ns):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"No access to namespace '{ns}'")
