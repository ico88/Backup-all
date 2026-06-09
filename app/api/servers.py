from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import json

from app.database import get_db
from app.models import Server, VMwareHost, ServerType
from app.crypto import encrypt, decrypt

router = APIRouter(prefix="/api/servers", tags=["servers"])


class VMwareHostCreate(BaseModel):
    name: str
    host: str
    port: int = 443
    username: str
    password: str
    ssl_verify: bool = False


class ServerCreate(BaseModel):
    name: str
    description: Optional[str] = None
    server_type: ServerType
    ip_address: str
    vm_name: Optional[str] = None
    vmware_host_id: Optional[int] = None
    ssh_port: int = 22
    winrm_port: int = 5985
    username: Optional[str] = None
    password: Optional[str] = None
    ssh_key_path: Optional[str] = None
    app_name: Optional[str] = None
    app_data_paths: Optional[list[str]] = None
    app_db_type: Optional[str] = None
    app_db_name: Optional[str] = None
    app_db_user: Optional[str] = None
    app_db_password: Optional[str] = None


# ── VMware Hosts ──────────────────────────────────────

@router.get("/vmware-hosts")
def list_vmware_hosts(db: Session = Depends(get_db)):
    hosts = db.query(VMwareHost).all()
    return [{"id": h.id, "name": h.name, "host": h.host, "port": h.port,
             "username": h.username} for h in hosts]


@router.post("/vmware-hosts", status_code=201)
def create_vmware_host(data: VMwareHostCreate, db: Session = Depends(get_db)):
    host = VMwareHost(
        name=data.name, host=data.host, port=data.port,
        username=data.username, password_enc=encrypt(data.password),
        ssl_verify=data.ssl_verify,
    )
    db.add(host)
    db.commit()
    db.refresh(host)
    return {"id": host.id, "name": host.name}


@router.get("/vmware-hosts/{host_id}/vms")
def list_vms_on_host(host_id: int, db: Session = Depends(get_db)):
    host = db.get(VMwareHost, host_id)
    if not host:
        raise HTTPException(404, "Host VMware non trovato")
    from app.backup.vmware import list_vms
    try:
        return list_vms(host)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Servers ───────────────────────────────────────────

@router.get("")
def list_servers(db: Session = Depends(get_db)):
    servers = db.query(Server).all()
    return [_serialize(s) for s in servers]


@router.get("/{server_id}")
def get_server(server_id: int, db: Session = Depends(get_db)):
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "Server non trovato")
    return _serialize(s)


@router.post("", status_code=201)
def create_server(data: ServerCreate, db: Session = Depends(get_db)):
    s = Server(
        name=data.name,
        description=data.description,
        server_type=data.server_type,
        ip_address=data.ip_address,
        vm_name=data.vm_name,
        vmware_host_id=data.vmware_host_id,
        ssh_port=data.ssh_port,
        winrm_port=data.winrm_port,
        username=data.username,
        password_enc=encrypt(data.password) if data.password else None,
        ssh_key_path=data.ssh_key_path,
        app_name=data.app_name,
        app_data_paths=json.dumps(data.app_data_paths or []),
        app_db_type=data.app_db_type,
        app_db_name=data.app_db_name,
        app_db_user=data.app_db_user,
        app_db_password_enc=encrypt(data.app_db_password) if data.app_db_password else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "name": s.name}


@router.delete("/{server_id}")
def delete_server(server_id: int, db: Session = Depends(get_db)):
    s = db.get(Server, server_id)
    if not s:
        raise HTTPException(404, "Server non trovato")
    db.delete(s)
    db.commit()
    return {"ok": True}


def _serialize(s: Server) -> dict:
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "server_type": s.server_type, "ip_address": s.ip_address,
        "vm_name": s.vm_name, "vmware_host_id": s.vmware_host_id,
        "app_name": s.app_name,
        "app_data_paths": json.loads(s.app_data_paths or "[]"),
        "app_db_type": s.app_db_type, "app_db_name": s.app_db_name,
        "is_active": s.is_active,
    }
