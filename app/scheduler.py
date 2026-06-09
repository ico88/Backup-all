"""APScheduler: carica i job attivi dal DB e li schedula con espressione cron."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import BackupJob, JobStatus

scheduler = BackgroundScheduler(timezone="Europe/Rome")


def _execute_job(job_id: int):
    from app.backup.engine import run_job
    db: Session = SessionLocal()
    try:
        run_job(job_id, db, triggered_by="scheduler")
    except Exception as e:
        print(f"[SCHEDULER] Job {job_id} fallito: {e}")
    finally:
        db.close()


def load_jobs_from_db():
    """Ricarica tutti i job attivi dal database nello scheduler."""
    db: Session = SessionLocal()
    try:
        jobs = db.query(BackupJob).filter(BackupJob.status == JobStatus.ACTIVE).all()
        # Rimuovi job esistenti e ricarica
        for apj in scheduler.get_jobs():
            apj.remove()
        for job in jobs:
            parts = job.cron_expression.split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
            else:
                continue
            scheduler.add_job(
                _execute_job,
                trigger=CronTrigger(
                    minute=minute, hour=hour,
                    day=day, month=month, day_of_week=day_of_week,
                    timezone="Europe/Rome",
                ),
                args=[job.id],
                id=f"job_{job.id}",
                replace_existing=True,
                name=job.name,
            )
        print(f"[SCHEDULER] {len(jobs)} job caricati")
    finally:
        db.close()


def start():
    scheduler.start()
    load_jobs_from_db()


def stop():
    scheduler.shutdown(wait=False)
