from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BackupRun, BackupLog

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
def list_runs(job_id: int = None, limit: int = 50, db: Session = Depends(get_db)):
    q = db.query(BackupRun).order_by(BackupRun.started_at.desc())
    if job_id:
        q = q.filter(BackupRun.job_id == job_id)
    runs = q.limit(limit).all()
    return [_serialize(r) for r in runs]


@router.get("/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    r = db.get(BackupRun, run_id)
    if not r:
        raise HTTPException(404, "Run non trovata")
    return _serialize(r)


@router.get("/{run_id}/logs")
def get_run_logs(run_id: int, db: Session = Depends(get_db)):
    logs = db.query(BackupLog).filter(BackupLog.run_id == run_id).order_by(BackupLog.timestamp).all()
    return [{"timestamp": l.timestamp.isoformat(), "level": l.level, "message": l.message}
            for l in logs]


def _serialize(r: BackupRun) -> dict:
    return {
        "id": r.id,
        "job_id": r.job_id,
        "job_name": r.job.name if r.job else None,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "size_bytes": r.size_bytes,
        "backup_path": r.backup_path,
        "error_message": r.error_message,
        "triggered_by": r.triggered_by,
        "checksum_sha256": r.checksum_sha256,
        "integrity_verified": r.integrity_verified,
        "backup_mode": r.backup_mode or "full",
    }
