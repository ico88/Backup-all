from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from app.database import get_db
from app.auth import hash_password, verify_password, login_user, logout_user
from app.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    email: str = ""
    password: str
    is_admin: bool = False


@router.post("/login")
def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=data.username, is_active=True).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "Credenziali non valide")
    login_user(request, user.id)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "username": user.username, "is_admin": user.is_admin}


@router.post("/logout")
def logout(request: Request):
    logout_user(request)
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Utente non trovato")
    return {"id": user.id, "username": user.username, "email": user.email,
            "is_admin": user.is_admin, "last_login_at": user.last_login_at}


@router.post("/change-password")
def change_password(data: ChangePasswordRequest, request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    user = db.get(User, uid)
    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(400, "Password attuale non corretta")
    user.hashed_password = hash_password(data.new_password)
    db.commit()
    return {"ok": True}


@router.get("/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    caller = db.get(User, uid)
    if not caller or not caller.is_admin:
        raise HTTPException(403, "Solo gli amministratori possono gestire gli utenti")
    users = db.query(User).all()
    return [{"id": u.id, "username": u.username, "email": u.email,
             "is_admin": u.is_admin, "is_active": u.is_active,
             "last_login_at": u.last_login_at} for u in users]


@router.post("/users", status_code=201)
def create_user(data: CreateUserRequest, request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    caller = db.get(User, uid)
    if not caller or not caller.is_admin:
        raise HTTPException(403, "Solo gli amministratori possono creare utenti")
    if db.query(User).filter_by(username=data.username).first():
        raise HTTPException(400, "Username già esistente")
    user = User(username=data.username, email=data.email,
                hashed_password=hash_password(data.password), is_admin=data.is_admin)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    caller = db.get(User, uid)
    if not caller or not caller.is_admin:
        raise HTTPException(403, "Solo gli amministratori possono eliminare utenti")
    if user_id == uid:
        raise HTTPException(400, "Non puoi eliminare te stesso")
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Utente non trovato")
    db.delete(user)
    db.commit()
    return {"ok": True}
