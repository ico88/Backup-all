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
    dest_type: str
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


@router.put("/{dest_id}")
def update_destination(dest_id: int, data: DestinationCreate, db: Session = Depends(get_db)):
    d = db.get(BackupDestination, dest_id)
    if not d:
        raise HTTPException(404, "Destinazione non trovata")
    d.name = data.name
    d.description = data.description
    d.dest_type = data.dest_type
    d.host = data.host
    d.port = data.port
    d.username = data.username
    if data.password:
        d.password_enc = encrypt(data.password)
    d.base_path = data.base_path
    d.rsync_module = data.rsync_module
    d.max_retention_days = data.max_retention_days
    db.commit()
    db.refresh(d)
    return _serialize(d)


@router.post("/{dest_id}/test")
def test_destination(dest_id: int, db: Session = Depends(get_db)):
    import subprocess, os, shutil, logging
    from app.crypto import decrypt
    log = logging.getLogger("backup-all.test")

    d = db.get(BackupDestination, dest_id)
    if not d:
        raise HTTPException(404, "Destinazione non trovata")

    password = decrypt(d.password_enc) if d.password_enc else ""
    port = d.port or 22

    has_sshpass = shutil.which("sshpass") is not None
    if password and not has_sshpass:
        raise HTTPException(500, "sshpass non installato sul server. Esegui: sudo apt install sshpass")

    env = os.environ.copy()
    if password and has_sshpass:
        env["SSHPASS"] = password
        cmd = ["sshpass", "-e", "ssh"]
    else:
        cmd = ["ssh"]

    cmd += ["-p", str(port), "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no",
            f"{d.username}@{d.host}", "echo OK"]

    log.info("Test QNAP: %s@%s:%s", d.username, d.host, port)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    except subprocess.TimeoutExpired:
        raise HTTPException(500, f"Timeout: {d.host}:{port} non risponde entro 15 secondi")
    except FileNotFoundError as e:
        raise HTTPException(500, f"Comando non trovato: {e}")

    log.info("rc=%d stdout=%r stderr=%r", result.returncode, result.stdout[:200], result.stderr[:200])

    if result.returncode == 0 and "OK" in result.stdout:
        return {"ok": True, "message": f"Connessione SSH a {d.host}:{port} riuscita ✓"}

    stderr = result.stderr.strip()
    if "Permission denied" in stderr or "Authentication failed" in stderr:
        raise HTTPException(500, f"Credenziali errate per {d.username}@{d.host}")
    elif "Connection refused" in stderr:
        raise HTTPException(500, f"Connessione rifiutata su {d.host}:{port} — SSH abilitato sul QNAP?")
    elif "No route to host" in stderr or "Network unreachable" in stderr:
        raise HTTPException(500, f"Host {d.host} non raggiungibile — controlla IP e rete")
    elif "Connection timed out" in stderr:
        raise HTTPException(500, f"Timeout connessione a {d.host}:{port}")
    raise HTTPException(500, f"SSH error (rc={result.returncode}): {stderr[:300] or 'nessun output'}")


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
