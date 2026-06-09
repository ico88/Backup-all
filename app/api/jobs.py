from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import BackupJob, BackupType, JobStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    name: str
    description: Optional[str] = None
    server_id: int
    destination_id: int
    backup_type: BackupType
    cron_expression: str
    retention_copies: int = 7
    compression: bool = True
    notify_email: Optional[str] = None


@router.get("")
def list_jobs(db: Session = Depends(get_db)):
    jobs = db.query(BackupJob).all()
    return [_serialize(j) for j in jobs]


@router.post("", status_code=201)
def create_job(data: JobCreate, db: Session = Depends(get_db)):
    job = BackupJob(
        name=data.name, description=data.description,
        server_id=data.server_id, destination_id=data.destination_id,
        backup_type=data.backup_type, cron_expression=data.cron_expression,
        retention_copies=data.retention_copies, compression=data.compression,
        notify_email=data.notify_email,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    # Ricarica scheduler
    from app.scheduler import load_jobs_from_db
    load_jobs_from_db()
    return {"id": job.id, "name": job.name}


@router.post("/{job_id}/run")
def run_job_now(job_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Avvia manualmente un job in background."""
    job = db.get(BackupJob, job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")

    def _run():
        from app.backup.engine import run_job
        from app.database import SessionLocal
        session = SessionLocal()
        try:
            run_job(job_id, session, triggered_by="manual")
        finally:
            session.close()

    background_tasks.add_task(_run)
    return {"ok": True, "message": f"Job '{job.name}' avviato in background"}


@router.patch("/{job_id}/status")
def set_job_status(job_id: int, status: JobStatus, db: Session = Depends(get_db)):
    job = db.get(BackupJob, job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    job.status = status
    db.commit()
    from app.scheduler import load_jobs_from_db
    load_jobs_from_db()
    return {"ok": True}


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(BackupJob, job_id)
    if not job:
        raise HTTPException(404, "Job non trovato")
    db.delete(job)
    db.commit()
    from app.scheduler import load_jobs_from_db
    load_jobs_from_db()
    return {"ok": True}


def _serialize(j: BackupJob) -> dict:
    return {
        "id": j.id, "name": j.name, "description": j.description,
        "server_id": j.server_id,
        "server_name": j.server.name if j.server else None,
        "destination_id": j.destination_id,
        "destination_name": j.destination.name if j.destination else None,
        "backup_type": j.backup_type, "status": j.status,
        "cron_expression": j.cron_expression,
        "retention_copies": j.retention_copies,
        "last_run_at": j.last_run_at.isoformat() if j.last_run_at else None,
        "last_run_status": j.last_run_status,
    }
