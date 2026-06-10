"""Orchestratore backup: coordina VMware, Linux/Windows e QNAP."""
import hashlib
import os
import shutil
import tempfile
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import BackupJob, BackupRun, BackupLog, BackupType, RunStatus
from app.backup import vmware, linux, windows, qnap


def _export_vm(host_cfg, vm_name: str, dest_dir: str, log_fn):
    """
    Export VM con fallback automatico per ESXi Free License:
    1. Prova API pyvmomi ExportVm (richiede licenza)
    2. Se RestrictedVersion → download diretto dal datastore HTTPS (funziona su Free)
    3. Se datastore download fallisce → prova ovftool (se installato)
    """
    try:
        vmware.export_vm_ovf(host_cfg, vm_name, dest_dir, log_fn)
        return
    except Exception as e:
        if "RestrictedVersion" not in str(e) and "Current license" not in str(e):
            raise
        log_fn("ESXi licenza Free — API export non disponibile, uso download datastore diretto...", "WARNING")

    try:
        vmware.export_vm_datastore(host_cfg, vm_name, dest_dir, log_fn)
        return
    except Exception as e2:
        log_fn(f"Download datastore fallito: {e2} — provo ovftool...", "WARNING")

    vmware.export_vm_ovf_ovftool(host_cfg, vm_name, dest_dir, log_fn)


def _log(db: Session, run: BackupRun, message: str, level: str = "INFO"):
    entry = BackupLog(run_id=run.id, message=message, level=level)
    db.add(entry)
    db.commit()
    print(f"[{level}] {message}")


def _sha256_dir(path: str) -> str:
    """Calcola SHA-256 combinato di tutti i file in una directory."""
    h = hashlib.sha256()
    for root, _, files in sorted(os.walk(path)):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            h.update(os.path.relpath(fpath, path).encode())
            with open(fpath, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
    return h.hexdigest()


def _apply_count_retention(dest_cfg, server_name: str, max_copies: int, log_fn=None):
    """Mantiene al massimo max_copies backup per job sul QNAP (per conteggio)."""
    qnap.apply_count_retention(dest_cfg, server_name, max_copies, log_fn)


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

        from app.models import ServerType

        # ── SORGENTE VMWARE: snapshot + OVF export ────────────────
        if server.server_type == ServerType.VMWARE:
            if not server.vm_name or not server.vmware_host:
                log("vm_name o vmware_host non configurati, skip snapshot", "WARNING")
            else:
                snap_name = f"backup_{timestamp}"
                vm_dir = os.path.join(tmp_dir, "vm_export")
                os.makedirs(vm_dir)
                # Prova snapshot (richiede licenza ESXi non-free)
                snap_created = False
                try:
                    log(f"Creazione snapshot VMware '{snap_name}'...")
                    vmware.create_snapshot(server.vmware_host, server.vm_name, snap_name, log)
                    snap_created = True
                except Exception as snap_err:
                    err_str = str(snap_err)
                    if "RestrictedVersion" in err_str or "Current license" in err_str:
                        log("ESXi licenza Free: snapshot API non supportati, export OVF diretto...", "WARNING")
                    else:
                        raise
                log("Export OVF in corso (può richiedere diversi minuti)...")
                _export_vm(server.vmware_host, server.vm_name, vm_dir, log)
                if snap_created:
                    log("Rimozione snapshot temporaneo...")
                    vmware.remove_snapshot(server.vmware_host, server.vm_name, snap_name, log)

        else:
            # ── SNAPSHOT opzionale per Linux/Windows su VMware ───────
            if job.backup_type in (BackupType.VM_SNAPSHOT, BackupType.FULL):
                if server.vm_name and server.vmware_host:
                    snap_name = f"backup_{timestamp}"
                    vm_dir = os.path.join(tmp_dir, "vm_export")
                    os.makedirs(vm_dir)
                    snap_created = False
                    try:
                        log(f"Creazione snapshot VMware '{snap_name}'...")
                        vmware.create_snapshot(server.vmware_host, server.vm_name, snap_name, log)
                        snap_created = True
                    except Exception as snap_err:
                        err_str = str(snap_err)
                        if "RestrictedVersion" in err_str or "Current license" in err_str:
                            log("ESXi licenza Free: snapshot API non supportati, export OVF diretto...", "WARNING")
                        else:
                            raise
                    log("Export VM OVF...")
                    _export_vm(server.vmware_host, server.vm_name, vm_dir, log)
                    if snap_created:
                        log("Rimozione snapshot temporaneo...")
                        vmware.remove_snapshot(server.vmware_host, server.vm_name, snap_name, log)

            # ── BACKUP SISTEMA WINDOWS (bare-metal wbadmin) ──────────
            if (job.backup_type == BackupType.FULL
                    and server.server_type == ServerType.WINDOWS
                    and dest_cfg.smb_share):
                log("Backup sistema Windows via wbadmin (bare-metal)...")
                windows.backup_system_wbadmin(server, dest_cfg, timestamp, log)
                run.status = RunStatus.SUCCESS
                run.backup_path = f"{server.name}/{timestamp}"
                run.finished_at = datetime.now(timezone.utc)
                job.last_run_at = run.finished_at
                job.last_run_status = RunStatus.SUCCESS
                db.commit()
                log("Backup sistema Windows completato.")
                return run

        # ── DATI APPLICATIVI (solo Linux/Windows) ────────────────
        if (server.server_type != ServerType.VMWARE
                and job.backup_type in (BackupType.APP_DATA, BackupType.FULL)):
            app_dir = os.path.join(tmp_dir, "app_data")
            os.makedirs(app_dir)

            if server.server_type == ServerType.LINUX:
                log("Backup dati Linux...")
                linux.backup_app_data(server, app_dir, log)
                if server.app_db_type:
                    linux.backup_database(server, app_dir, log)
            elif server.server_type == ServerType.WINDOWS:
                log("Backup dati Windows...")
                windows.backup_app_data(server, app_dir, log)
                if server.app_db_type == "mssql":
                    windows.backup_mssql(server, app_dir, log)

        # ── VERIFICA INTEGRITÀ (pre-trasferimento) ────────────────
        if job.verify_integrity:
            log("Calcolo checksum SHA-256 del backup locale...")
            local_checksum = _sha256_dir(tmp_dir)
            run.checksum_sha256 = local_checksum
            db.commit()
            log(f"Checksum: {local_checksum[:16]}…")

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

        # ── VERIFICA INTEGRITÀ (post-trasferimento) ───────────────
        if job.verify_integrity and run.checksum_sha256:
            log("Verifica integrità post-trasferimento via SSH...")
            try:
                remote_checksum = qnap.compute_remote_checksum(
                    dest_cfg,
                    os.path.join(dest_cfg.base_path, remote_subpath)
                )
                if remote_checksum and remote_checksum == run.checksum_sha256:
                    run.integrity_verified = True
                    log("Integrità verificata: checksum corrispondente ✓")
                else:
                    run.integrity_verified = False
                    log(f"ATTENZIONE: checksum non corrispondente! locale={run.checksum_sha256[:16]} remoto={str(remote_checksum)[:16]}", "WARNING")
            except Exception as e:
                log(f"Verifica integrità remota non disponibile: {e}", "WARNING")
                run.integrity_verified = None
            db.commit()

        # ── RETENTION (per numero copie) ──────────────────────────
        if job.retention_copies and job.retention_copies > 0:
            log(f"Applicazione retention: max {job.retention_copies} copie...")
            _apply_count_retention(dest_cfg, server.name, job.retention_copies, log)

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

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── NOTIFICA EMAIL ────────────────────────────────────────────
    try:
        from app.notifications import notify_backup_result
        notify_backup_result(db, job, run)
    except Exception as e:
        print(f"[WARN] Notifica email fallita: {e}")

    return run
