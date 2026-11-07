"""APScheduler: carica i job attivi dal DB e li schedula con espressione cron."""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import BackupJob, JobStatus, VMReplicationJob, ReplicationStatus

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


def _execute_replication(job_id: int):
    from app.api.replication import _execute_sync
    _execute_sync(job_id, triggered_by="scheduler")


def _heartbeat_check():
    """Controlla lo stato di tutti i job di replica con auto_failover attivo."""
    from app.backup.replication import check_heartbeat
    from app.models import FailoverState
    from app.api.replication import _execute_promote
    from datetime import datetime, timezone

    db: Session = SessionLocal()
    try:
        jobs = db.query(VMReplicationJob).filter(
            VMReplicationJob.auto_failover == True,
            VMReplicationJob.status == ReplicationStatus.ACTIVE,
            VMReplicationJob.failover_state == FailoverState.NORMAL,
        ).all()

        for j in jobs:
            alive = check_heartbeat(j)
            j.last_heartbeat_at = datetime.now(timezone.utc)
            if alive:
                if j.heartbeat_failures > 0:
                    print(f"[HEARTBEAT] {j.source_vm_name} tornata online, reset failures")
                j.heartbeat_failures = 0
            else:
                j.heartbeat_failures += 1
                print(f"[HEARTBEAT] {j.source_vm_name} irraggiungibile "
                      f"({j.heartbeat_failures}/{j.heartbeat_max_failures})")
                if j.heartbeat_failures >= j.heartbeat_max_failures:
                    print(f"[HEARTBEAT] FAILOVER AUTOMATICO: promuovo {j.target_vm_name}")
                    db.commit()
                    _execute_promote(j.id, triggered_by="heartbeat_failover")
                    # Ricarica il job dal DB per aggiornare stato
                    db.refresh(j)
                    continue
            db.commit()
    except Exception as e:
        print(f"[HEARTBEAT] Errore: {e}")
    finally:
        db.close()


def load_jobs_from_db():
    """Ricarica tutti i backup job attivi dal database nello scheduler."""
    db: Session = SessionLocal()
    try:
        jobs = db.query(BackupJob).filter(BackupJob.status == JobStatus.ACTIVE).all()
        for apj in scheduler.get_jobs():
            if apj.id.startswith("job_"):
                apj.remove()
        for job in jobs:
            parts = job.cron_expression.split()
            if len(parts) != 5:
                continue
            minute, hour, day, month, day_of_week = parts
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
        print(f"[SCHEDULER] {len(jobs)} backup job caricati")
    finally:
        db.close()


def load_replication_jobs():
    """Ricarica tutti i replication job attivi."""
    db: Session = SessionLocal()
    try:
        jobs = db.query(VMReplicationJob).filter(
            VMReplicationJob.status == ReplicationStatus.ACTIVE
        ).all()
        for apj in scheduler.get_jobs():
            if apj.id.startswith("repl_"):
                apj.remove()
        for job in jobs:
            parts = job.cron_expression.split()
            if len(parts) != 5:
                continue
            minute, hour, day, month, day_of_week = parts
            scheduler.add_job(
                _execute_replication,
                trigger=CronTrigger(
                    minute=minute, hour=hour,
                    day=day, month=month, day_of_week=day_of_week,
                    timezone="Europe/Rome",
                ),
                args=[job.id],
                id=f"repl_{job.id}",
                replace_existing=True,
                name=f"Replica: {job.name}",
            )
        print(f"[SCHEDULER] {len(jobs)} replication job caricati")
    finally:
        db.close()


def start():
    scheduler.start()
    load_jobs_from_db()
    load_replication_jobs()
    # Heartbeat ogni 60 secondi
    scheduler.add_job(
        _heartbeat_check,
        trigger=IntervalTrigger(seconds=60),
        id="heartbeat_monitor",
        replace_existing=True,
        name="Heartbeat Monitor",
    )
    print("[SCHEDULER] Heartbeat monitor avviato")


def stop():
    scheduler.shutdown(wait=False)
