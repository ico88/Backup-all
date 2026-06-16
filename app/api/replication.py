from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
import time

from app.database import get_db
from app.models import VMReplicationJob, VMReplicationRun, ReplicationStatus, ReplicationSyncStatus, FailoverState

router = APIRouter(prefix="/api/replication", tags=["replication"])
RUN_LOG_TAIL_CHARS = 50000
LIVE_LOG_FLUSH_SEC = 3
LIVE_LOG_FLUSH_LINES = 20


class ReplicationCreate(BaseModel):
    name: str
    description: Optional[str] = None
    source_host_id: int
    source_vm_name: str
    target_host_id: int
    target_vm_name: str
    target_datastore: Optional[str] = None
    cron_expression: str = "0 2 * * *"
    auto_failover: bool = False
    heartbeat_interval_sec: int = 60
    heartbeat_max_failures: int = 3
    source_vm_ip: Optional[str] = None


@router.get("")
def list_jobs(db: Session = Depends(get_db)):
    jobs = db.query(VMReplicationJob).all()
    return [_serialize(j) for j in jobs]


@router.get("/ovftool-check")
def ovftool_check():
    try:
        from app.backup.replication import _find_ovftool
        return {"available": True, "path": _find_ovftool()}
    except Exception as e:
        return {"available": False, "error": str(e)}


@router.post("", status_code=201)
def create_job(data: ReplicationCreate, db: Session = Depends(get_db)):
    j = VMReplicationJob(**data.model_dump())
    db.add(j)
    db.commit()
    db.refresh(j)
    _reload_scheduler()
    return {"id": j.id, "name": j.name}


@router.put("/{job_id}")
def update_job(job_id: int, data: ReplicationCreate, db: Session = Depends(get_db)):
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    for k, v in data.model_dump().items():
        setattr(j, k, v)
    db.commit()
    _reload_scheduler()
    return {"ok": True}


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    db.query(VMReplicationRun).filter_by(job_id=job_id).delete(synchronize_session=False)
    db.delete(j)
    db.commit()
    _reload_scheduler()
    return {"ok": True}


@router.post("/{job_id}/pause")
def pause_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    j.status = ReplicationStatus.PAUSED
    db.commit()
    _reload_scheduler()
    return {"ok": True}


@router.post("/{job_id}/resume")
def resume_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    j.status = ReplicationStatus.ACTIVE
    db.commit()
    _reload_scheduler()
    return {"ok": True}


@router.post("/{job_id}/run")
def run_now(job_id: int, db: Session = Depends(get_db)):
    """Avvia sync manuale immediato."""
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    if j.last_sync_status == ReplicationSyncStatus.RUNNING:
        raise HTTPException(409, "Esiste già una replica in corso o rimasta in stato running. Interrompila prima di avviarne una nuova.")
    import threading
    threading.Thread(target=_execute_sync, args=(job_id, "manual"), daemon=True).start()
    return {"ok": True, "message": "Sync avviato in background"}


@router.post("/{job_id}/cancel")
def cancel_run(job_id: int, db: Session = Depends(get_db)):
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    from app.backup.replication import cancel_sync
    if not cancel_sync(job_id):
        stale_run = db.query(VMReplicationRun).filter_by(
            job_id=job_id,
            status=ReplicationSyncStatus.RUNNING,
        ).order_by(VMReplicationRun.started_at.desc()).first()
        if not stale_run and j.last_sync_status != ReplicationSyncStatus.RUNNING:
            raise HTTPException(409, "Nessuna replica in corso da interrompere")
        message = (
            "Replica segnata come interrotta: nessun processo ovftool attivo risulta "
            "registrato dall'app. Se il servizio e' stato riavviato, controlla manualmente "
            "eventuali processi ovftool rimasti sul server."
        )
        if stale_run:
            stale_run.status = ReplicationSyncStatus.FAILED
            stale_run.finished_at = datetime.now(timezone.utc)
            stale_run.error_message = message
            stale_run.log_output = ((stale_run.log_output or "") + f"\n[WARNING] {message}").strip()
        j.last_sync_status = ReplicationSyncStatus.FAILED
        db.commit()
        return {"ok": True, "message": message}
    return {"ok": True, "message": "Interruzione replica richiesta"}


@router.post("/{job_id}/promote")
def promote(job_id: int, db: Session = Depends(get_db)):
    """Failover manuale: promuove VM standby a primaria."""
    j = db.get(VMReplicationJob, job_id)
    if not j:
        raise HTTPException(404, "Job non trovato")
    import threading
    threading.Thread(target=_execute_promote, args=(job_id, "manual"), daemon=True).start()
    return {"ok": True, "message": "Failover avviato"}


@router.get("/{job_id}/runs")
def get_runs(job_id: int, db: Session = Depends(get_db)):
    runs = db.query(VMReplicationRun).filter_by(job_id=job_id).order_by(
        VMReplicationRun.started_at.desc()).limit(20).all()
    return [_serialize_run(r) for r in runs]


def _serialize(j: VMReplicationJob) -> dict:
    return {
        "id": j.id, "name": j.name, "description": j.description,
        "source_host_id": j.source_host_id,
        "source_host_name": j.source_host.name if j.source_host else None,
        "source_host_ip": j.source_host.host if j.source_host else None,
        "source_vm_name": j.source_vm_name,
        "target_host_id": j.target_host_id,
        "target_host_name": j.target_host.name if j.target_host else None,
        "target_host_ip": j.target_host.host if j.target_host else None,
        "target_vm_name": j.target_vm_name,
        "target_datastore": j.target_datastore,
        "cron_expression": j.cron_expression,
        "status": j.status,
        "auto_failover": j.auto_failover,
        "heartbeat_interval_sec": j.heartbeat_interval_sec,
        "heartbeat_max_failures": j.heartbeat_max_failures,
        "heartbeat_failures": j.heartbeat_failures,
        "source_vm_ip": j.source_vm_ip,
        "failover_state": j.failover_state,
        "last_sync_at": j.last_sync_at.isoformat() if j.last_sync_at else None,
        "last_sync_status": j.last_sync_status,
        "last_sync_duration_sec": j.last_sync_duration_sec,
        "last_heartbeat_at": j.last_heartbeat_at.isoformat() if j.last_heartbeat_at else None,
    }


def _serialize_run(r: VMReplicationRun) -> dict:
    log_output = r.log_output
    if log_output and len(log_output) > RUN_LOG_TAIL_CHARS:
        log_output = (
            f"... log precedente omesso, mostro gli ultimi {RUN_LOG_TAIL_CHARS} caratteri ...\n"
            + log_output[-RUN_LOG_TAIL_CHARS:]
        )
    return {
        "id": r.id, "job_id": r.job_id,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status, "triggered_by": r.triggered_by,
        "error_message": r.error_message,
        "log_output": log_output,
    }


def _execute_sync(job_id: int, triggered_by: str = "scheduler"):
    from app.database import SessionLocal
    from app.backup.replication import sync_vm
    db = SessionLocal()
    try:
        j = db.get(VMReplicationJob, job_id)
        if not j:
            return
        run = VMReplicationRun(job_id=job_id, triggered_by=triggered_by,
                               status=ReplicationSyncStatus.RUNNING)
        db.add(run)
        db.commit()
        db.refresh(run)

        j.last_sync_status = ReplicationSyncStatus.RUNNING
        db.commit()

        logs = []
        last_flush = time.monotonic()
        start = datetime.now(timezone.utc)

        def append_live_log(message: str, force: bool = False):
            nonlocal last_flush
            logs.append(message)
            now = time.monotonic()
            should_flush = (
                force
                or len(logs) % LIVE_LOG_FLUSH_LINES == 0
                or now - last_flush >= LIVE_LOG_FLUSH_SEC
            )
            if should_flush:
                run.log_output = "\n".join(logs)
                db.commit()
                last_flush = now

        try:
            output = sync_vm(j, log_fn=append_live_log)
            run.status = ReplicationSyncStatus.SUCCESS
            run.log_output = "\n".join(logs)
            j.last_sync_status = ReplicationSyncStatus.SUCCESS
            j.last_sync_at = datetime.now(timezone.utc)
            j.last_sync_duration_sec = int((datetime.now(timezone.utc) - start).total_seconds())
            j.heartbeat_failures = 0  # reset su sync OK
        except Exception as e:
            run.status = ReplicationSyncStatus.FAILED
            run.error_message = str(e)
            append_live_log(f"[ERROR] {e}", force=True)
            run.log_output = "\n".join(logs)
            j.last_sync_status = ReplicationSyncStatus.FAILED
        finally:
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def _execute_promote(job_id: int, triggered_by: str = "manual"):
    from app.database import SessionLocal
    from app.backup.replication import promote_standby
    db = SessionLocal()
    try:
        j = db.get(VMReplicationJob, job_id)
        if not j:
            return
        run = VMReplicationRun(job_id=job_id, triggered_by=triggered_by,
                               status=ReplicationSyncStatus.RUNNING)
        db.add(run)
        db.commit()
        logs = []
        try:
            promote_standby(j, log_fn=lambda m: logs.append(m))
            j.failover_state = FailoverState.FAILOVER
            j.status = ReplicationStatus.PAUSED  # ferma ulteriori repliche automatiche
            run.status = ReplicationSyncStatus.SUCCESS
            run.log_output = "\n".join(logs)
        except Exception as e:
            run.status = ReplicationSyncStatus.FAILED
            run.error_message = str(e)
            run.log_output = "\n".join(logs)
        finally:
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def _reload_scheduler():
    try:
        from app.scheduler import load_replication_jobs
        load_replication_jobs()
    except Exception:
        pass
