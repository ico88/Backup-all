from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import BackupDestination
from app.crypto import encrypt

router = APIRouter(prefix="/api/destinations", tags=["destinations"])


class DestinationCreate(BaseModel):
    name: str
    description: Optional[str] = None
    dest_type: str   # "rsync", "qnap_api"
    host: str
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    base_path: str
    rsync_module: Optional[str] = None
    max_retention_days: int = 30


@router.get("")
def list_destinations(db: Session = Depends(get_db)):
    dests = db.query(BackupDestination).all()
    return [_serialize(d) for d in dests]


@router.post("", status_code=201)
def create_destination(data: DestinationCreate, db: Session = Depends(get_db)):
    d = BackupDestination(
        name=data.name, description=data.description,
        dest_type=data.dest_type, host=data.host, port=data.port,
        username=data.username,
        password_enc=encrypt(data.password) if data.password else None,
        base_path=data.base_path,
        rsync_module=data.rsync_module,
        max_retention_days=data.max_retention_days,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return {"id": d.id, "name": d.name}


@router.post("/{dest_id}/test")
def test_destination(dest_id: int, db: Session = Depends(get_db)):
    """Verifica la connessione al QNAP."""
    d = db.get(BackupDestination, dest_id)
    if not d:
        raise HTTPException(404, "Destinazione non trovata")
    import subprocess, os
    from app.crypto import decrypt
    password = decrypt(d.password_enc) if d.password_enc else ""
    port = d.port or 22
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        ssh_prefix = ["sshpass", "-e", "ssh"]
    else:
        ssh_prefix = ["ssh"]
    result = subprocess.run(
        ssh_prefix + ["-p", str(port), "-o", "ConnectTimeout=5",
                      "-o", "StrictHostKeyChecking=no",
                      f"{d.username}@{d.host}", "echo OK"],
        capture_output=True, text=True, env=env, timeout=10
    )
    if result.returncode == 0:
        return {"ok": True, "message": "Connessione al QNAP riuscita"}
    raise HTTPException(500, f"Connessione fallita: {result.stderr}")


@router.delete("/{dest_id}")
def delete_destination(dest_id: int, db: Session = Depends(get_db)):
    d = db.get(BackupDestination, dest_id)
    if not d:
        raise HTTPException(404, "Destinazione non trovata")
    db.delete(d)
    db.commit()
    return {"ok": True}


def _serialize(d: BackupDestination) -> dict:
    return {
        "id": d.id, "name": d.name, "description": d.description,
        "dest_type": d.dest_type, "host": d.host, "port": d.port,
        "username": d.username, "base_path": d.base_path,
        "max_retention_days": d.max_retention_days, "is_active": d.is_active,
    }
