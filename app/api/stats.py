"""Statistiche e report aggregati sui backup."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, case
from datetime import datetime, timezone, timedelta

from app.database import get_db
from app.models import BackupRun, BackupJob, Server, BackupDestination, RunStatus

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary")
def summary(days: int = 30, db: Session = Depends(get_db)):
    """Statistiche globali per il periodo richiesto."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    runs = db.query(BackupRun).filter(BackupRun.started_at >= since).all()

    total = len(runs)
    success = sum(1 for r in runs if r.status == RunStatus.SUCCESS)
    failed = sum(1 for r in runs if r.status == RunStatus.FAILED)
    partial = sum(1 for r in runs if r.status == RunStatus.PARTIAL)
    running = sum(1 for r in runs if r.status == RunStatus.RUNNING)

    durations = [
        (r.finished_at - r.started_at).total_seconds()
        for r in runs
        if r.finished_at and r.started_at and r.status == RunStatus.SUCCESS
    ]
    sizes = [r.size_bytes for r in runs if r.size_bytes and r.status == RunStatus.SUCCESS]

    integrity_ok = sum(1 for r in runs if r.integrity_verified is True)
    integrity_fail = sum(1 for r in runs if r.integrity_verified is False)

    return {
        "period_days": days,
        "total_runs": total,
        "success": success,
        "failed": failed,
        "partial": partial,
        "running": running,
        "success_rate": round(success / total * 100, 1) if total else 0,
        "total_size_bytes": sum(sizes),
        "avg_size_bytes": int(sum(sizes) / len(sizes)) if sizes else 0,
        "avg_duration_seconds": round(sum(durations) / len(durations)) if durations else 0,
        "integrity_ok": integrity_ok,
        "integrity_fail": integrity_fail,
        "jobs_count": db.query(BackupJob).count(),
        "servers_count": db.query(Server).count(),
        "destinations_count": db.query(BackupDestination).count(),
    }


@router.get("/by-job")
def by_job(days: int = 30, db: Session = Depends(get_db)):
    """Statistiche per singolo job."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    jobs = db.query(BackupJob).all()
    result = []
    for job in jobs:
        runs = [r for r in job.runs if r.started_at and r.started_at.replace(tzinfo=timezone.utc) >= since]
        if not runs and not job.last_run_at:
            runs_all = job.runs
        else:
            runs_all = runs

        total = len(runs)
        success = sum(1 for r in runs if r.status == RunStatus.SUCCESS)
        failed = sum(1 for r in runs if r.status == RunStatus.FAILED)
        sizes = [r.size_bytes for r in runs if r.size_bytes and r.status == RunStatus.SUCCESS]
        durations = [
            (r.finished_at - r.started_at).total_seconds()
            for r in runs
            if r.finished_at and r.started_at and r.status == RunStatus.SUCCESS
        ]
        result.append({
            "job_id": job.id,
            "job_name": job.name,
            "server_name": job.server.name if job.server else "—",
            "backup_type": job.backup_type,
            "status": job.status,
            "total_runs": total,
            "success": success,
            "failed": failed,
            "success_rate": round(success / total * 100, 1) if total else None,
            "avg_size_bytes": int(sum(sizes) / len(sizes)) if sizes else 0,
            "avg_duration_seconds": round(sum(durations) / len(durations)) if durations else 0,
            "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
            "last_run_status": job.last_run_status,
            "retention_copies": job.retention_copies,
            "verify_integrity": job.verify_integrity,
            "notify_email": job.notify_email,
        })
    return result


@router.get("/trend")
def trend(days: int = 30, db: Session = Depends(get_db)):
    """Trend giornaliero: successi/fallimenti per ciascun giorno."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    runs = db.query(BackupRun).filter(
        BackupRun.started_at >= since,
        BackupRun.status.in_([RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.PARTIAL])
    ).all()

    days_map: dict[str, dict] = {}
    for i in range(days):
        day = (datetime.now(timezone.utc) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        days_map[day] = {"date": day, "success": 0, "failed": 0, "partial": 0, "size_bytes": 0}

    for r in runs:
        day = r.started_at.strftime("%Y-%m-%d")
        if day in days_map:
            days_map[day][r.status.value] = days_map[day].get(r.status.value, 0) + 1
            if r.size_bytes:
                days_map[day]["size_bytes"] += r.size_bytes

    return list(days_map.values())


@router.get("/export-csv")
def export_csv(days: int = 30, db: Session = Depends(get_db)):
    """Esporta le esecuzioni in CSV."""
    from fastapi.responses import StreamingResponse
    import io, csv

    since = datetime.now(timezone.utc) - timedelta(days=days)
    runs = db.query(BackupRun).filter(BackupRun.started_at >= since).order_by(BackupRun.started_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Job", "Stato", "Avviato", "Terminato", "Durata (s)",
                     "Dimensione (MB)", "Integrità", "Percorso", "Avviato da", "Errore"])
    for r in runs:
        duration = ""
        if r.started_at and r.finished_at:
            duration = round((r.finished_at - r.started_at).total_seconds())
        size_mb = round(r.size_bytes / 1_048_576, 2) if r.size_bytes else ""
        integrity = {True: "OK", False: "FALLITA", None: ""}.get(r.integrity_verified, "")
        writer.writerow([
            r.id,
            r.job.name if r.job else f"#{r.job_id}",
            r.status.value,
            r.started_at.strftime("%d/%m/%Y %H:%M:%S") if r.started_at else "",
            r.finished_at.strftime("%d/%m/%Y %H:%M:%S") if r.finished_at else "",
            duration,
            size_mb,
            integrity,
            r.backup_path or "",
            r.triggered_by or "",
            r.error_message or "",
        ])

    output.seek(0)
    filename = f"backup-report-{datetime.now().strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
