"""Orchestratore backup: coordina VMware, Linux/Windows e QNAP."""
import os
import shutil
import tempfile
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import BackupJob, BackupRun, BackupLog, BackupType, RunStatus
from app.backup import vmware, linux, windows, qnap


def _log(db: Session, run: BackupRun, message: str, level: str = "INFO"):
    entry = BackupLog(run_id=run.id, message=message, level=level)
    db.add(entry)
    db.commit()
    print(f"[{level}] {message}")


def run_job(job_id: int, db: Session, triggered_by: str = "scheduler") -> BackupRun:
    job: BackupJob = db.get(BackupJob, job_id)
    if not job:
        raise ValueError(f"Job {job_id} non trovato")

    run = BackupRun(job_id=job.id, status=RunStatus.RUNNING, triggered_by=triggered_by)
    db.add(run)
    db.commit()
    db.refresh(run)

    log = lambda msg, level="INFO": _log(db, run, msg, level)

    tmp_dir = tempfile.mkdtemp(prefix="backup_")
    try:
        server = job.server
        dest_cfg = job.destination
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        remote_subpath = f"{server.name}/{timestamp}"

        # ── SNAPSHOT VM ──────────────────────────────────────────
        if job.backup_type in (BackupType.VM_SNAPSHOT, BackupType.FULL):
            if not server.vm_name or not server.vmware_host:
                log("vm_name o vmware_host non configurati, skip snapshot", "WARNING")
            else:
                snap_name = f"backup_{timestamp}"
                log(f"Creazione snapshot VMware '{snap_name}'...")
                vmware.create_snapshot(server.vmware_host, server.vm_name, snap_name, log)

                vm_dir = os.path.join(tmp_dir, "vm_export")
                os.makedirs(vm_dir)
                log("Export VM OVF...")
                vmware.export_vm_ovf(server.vmware_host, server.vm_name, vm_dir, log)

                log("Rimozione snapshot temporaneo...")
                vmware.remove_snapshot(server.vmware_host, server.vm_name, snap_name, log)

        # ── DATI APPLICATIVI ─────────────────────────────────────
        if job.backup_type in (BackupType.APP_DATA, BackupType.FULL):
            app_dir = os.path.join(tmp_dir, "app_data")
            os.makedirs(app_dir)

            from app.models import ServerType
            if server.server_type == ServerType.LINUX:
                log("Backup dati Abulafia (Linux)...")
                linux.backup_app_data(server, app_dir, log)
                if server.app_db_type:
                    linux.backup_database(server, app_dir, log)

            elif server.server_type == ServerType.WINDOWS:
                log("Backup dati Gamma/TeamSystem (Windows)...")
                windows.backup_app_data(server, app_dir, log)
                if server.app_db_type == "mssql":
                    windows.backup_mssql(server, app_dir, log)

        # ── TRASFERIMENTO SU QNAP ────────────────────────────────
        log(f"Invio backup su QNAP ({dest_cfg.dest_type})...")
        if dest_cfg.dest_type in ("rsync", "qnap_rsync"):
            bytes_sent = qnap.rsync_to_qnap(dest_cfg, tmp_dir, remote_subpath, log)
        elif dest_cfg.dest_type == "qnap_api":
            client = qnap.QNAPClient(dest_cfg)
            client.login()
            for fname in os.listdir(tmp_dir):
                client.upload_file(os.path.join(tmp_dir, fname),
                                   os.path.join(dest_cfg.base_path, remote_subpath), log)
            client.logout()
            bytes_sent = sum(
                os.path.getsize(os.path.join(tmp_dir, f))
                for f in os.listdir(tmp_dir)
                if os.path.isfile(os.path.join(tmp_dir, f))
            )
        else:
            raise ValueError(f"Tipo destinazione sconosciuto: {dest_cfg.dest_type}")

        # ── RETENTION ────────────────────────────────────────────
        if dest_cfg.max_retention_days:
            qnap.apply_retention(
                dest_cfg,
                os.path.join(dest_cfg.base_path, server.name),
                dest_cfg.max_retention_days,
                log
            )

        # ── SUCCESSO ─────────────────────────────────────────────
        run.status = RunStatus.SUCCESS
        run.size_bytes = bytes_sent
        run.backup_path = remote_subpath
        run.finished_at = datetime.now(timezone.utc)
        job.last_run_at = run.finished_at
        job.last_run_status = RunStatus.SUCCESS
        db.commit()
        log(f"Backup completato con successo ({bytes_sent:,} bytes)")

    except Exception as exc:
        run.status = RunStatus.FAILED
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = str(exc)
        job.last_run_at = run.finished_at
        job.last_run_status = RunStatus.FAILED
        db.commit()
        log(f"ERRORE: {exc}", "ERROR")
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return run
