from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SystemSettings

router = APIRouter(prefix="/api/settings", tags=["settings"])

SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_user", "smtp_password",
             "smtp_from", "smtp_tls"]


class SmtpSettings(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True


def _require_admin(request: Request, db: Session):
    from app.models import User
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(401, "Non autenticato")
    user = db.get(User, uid)
    if not user or not user.is_admin:
        raise HTTPException(403, "Accesso riservato agli amministratori")
    return user


def _get(db: Session, key: str) -> str:
    s = db.query(SystemSettings).filter_by(key=key).first()
    return s.value or "" if s else ""


def _set(db: Session, key: str, value: str):
    s = db.query(SystemSettings).filter_by(key=key).first()
    if s:
        s.value = value
    else:
        db.add(SystemSettings(key=key, value=value))
    db.commit()


@router.get("/smtp")
def get_smtp(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    return {
        "smtp_host": _get(db, "smtp_host"),
        "smtp_port": int(_get(db, "smtp_port") or 587),
        "smtp_user": _get(db, "smtp_user"),
        "smtp_password": "***" if _get(db, "smtp_password") else "",
        "smtp_from": _get(db, "smtp_from"),
        "smtp_tls": (_get(db, "smtp_tls") or "true").lower() == "true",
    }


@router.put("/smtp")
def save_smtp(data: SmtpSettings, request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    _set(db, "smtp_host", data.smtp_host)
    _set(db, "smtp_port", str(data.smtp_port))
    _set(db, "smtp_user", data.smtp_user)
    if data.smtp_password and data.smtp_password != "***":
        _set(db, "smtp_password", data.smtp_password)
    _set(db, "smtp_from", data.smtp_from)
    _set(db, "smtp_tls", "true" if data.smtp_tls else "false")
    return {"ok": True}


@router.post("/smtp/test")
def test_smtp(request: Request, db: Session = Depends(get_db)):
    _require_admin(request, db)
    from app.models import User
    uid = request.session.get("user_id")
    user = db.get(User, uid)
    email = user.email or "test@example.com"
    from app.notifications import send_email
    try:
        send_email(db, email, "Test SMTP — Backup-All CRI Catania",
                   "Se ricevi questa email, la configurazione SMTP è corretta.")
        return {"ok": True, "message": f"Email di test inviata a {email}"}
    except Exception as e:
        raise HTTPException(400, f"Invio email fallito: {e}")
