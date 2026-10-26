from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from backend.database import create_user, get_user

ROOT = Path(__file__).resolve().parent.parent


def _development_secret() -> str:
    secret_path = ROOT / ".healpipe-dev-secret"
    try:
        descriptor = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="ascii") as secret_file:
            secret_file.write(secrets.token_urlsafe(48))
    secret = secret_path.read_text(encoding="ascii").strip()
    if len(secret) < 32:
        raise RuntimeError(f"Development JWT secret at {secret_path} is invalid.")
    return secret


JWT_SECRET = os.getenv("HEALPIPE_JWT_SECRET")
if not JWT_SECRET:
    if os.getenv("HEALPIPE_ENV", "development").lower() == "production":
        raise RuntimeError("HEALPIPE_JWT_SECRET must be configured in production.")
    JWT_SECRET = _development_secret()
if os.getenv("HEALPIPE_ENV", "development").lower() == "production" and len(JWT_SECRET) < 32:
    raise RuntimeError("HEALPIPE_JWT_SECRET must be at least 32 characters in production.")
JWT_ALGORITHM = "HS256"
TOKEN_LIFETIME = timedelta(minutes=60)
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_text, digest_text = encoded.split("$", maxsplit=1)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def issue_token(username: str, user_id: int) -> str:
    expires = datetime.now(timezone.utc) + TOKEN_LIFETIME
    return jwt.encode({"sub": username, "uid": user_id, "exp": expires}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required.")
    try:
        claims = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = claims.get("sub")
        user_id = claims.get("uid")
        if not username or user_id is None:
            raise HTTPException(status_code=401, detail="Invalid access token.")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired access token.") from exc
    user = get_user(username)
    if user is None or user["id"] != user_id:
        raise HTTPException(status_code=401, detail="Account is no longer available.")
    return {"id": user["id"], "username": user["username"], "mfa_enabled": bool(user["mfa_enabled"])}


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def verify_totp(secret: str, code: str, timestamp: int | None = None) -> bool:
    if not code.isdigit() or len(code) != 6:
        return False
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int((timestamp if timestamp is not None else time.time()) // 30)
    for offset in (-1, 0, 1):
        digest = hmac.new(key, struct.pack(">Q", counter + offset), hashlib.sha1).digest()
        index = digest[-1] & 0x0F
        number = struct.unpack(">I", digest[index:index + 4])[0] & 0x7FFFFFFF
        if hmac.compare_digest(f"{number % 1_000_000:06d}", code):
            return True
    return False


def register(username: str, password: str) -> int:
    username = username.strip().lower()
    if len(username) < 3 or len(username) > 80:
        raise HTTPException(status_code=422, detail="Username must be between 3 and 80 characters.")
    if len(password) < 12:
        raise HTTPException(status_code=422, detail="Password must contain at least 12 characters.")
    try:
        return create_user(username, hash_password(password))
    except sqlite3.IntegrityError as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(status_code=409, detail="Username is already registered.") from exc
        raise
