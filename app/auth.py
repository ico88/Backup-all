"""Autenticazione: hashing password e gestione sessione."""
import bcrypt
from fastapi import Request, HTTPException


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def get_session_user_id(request: Request) -> int | None:
    return request.session.get("user_id")


def require_user(request: Request):
    uid = get_session_user_id(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Non autenticato")
    return uid


def login_user(request: Request, user_id: int):
    request.session["user_id"] = user_id


def logout_user(request: Request):
    request.session.clear()
