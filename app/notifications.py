"""Notifiche email via SMTP."""
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

from sqlalchemy.orm import Session


def _get_setting(db: Session, key: str) -> Optional[str]:
    from app.models import SystemSettings
    s = db.query(SystemSettings).filter_by(key=key).first()
    return s.value if s else None


def send_email(db: Session, to: str, subject: str, body: str):
    """Invia email usando le impostazioni SMTP salvate nel DB."""
    host = _get_setting(db, "smtp_host")
    if not host:
        return  # SMTP non configurato
    port = int(_get_setting(db, "smtp_port") or 587)
    user = _get_setting(db, "smtp_user") or ""
    password = _get_setting(db, "smtp_password") or ""
    from_addr = _get_setting(db, "smtp_from") or user
    use_tls = (_get_setting(db, "smtp_tls") or "true").lower() == "true"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to
    msg.set_content(body)

    ctx = ssl.create_default_context() if use_tls else None
    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls(context=ctx)
                if user:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=10) as s:
                if user:
                    s.login(user, password)
                s.send_message(msg)
    except Exception as exc:
        print(f"[WARN] Email non inviata a {to}: {exc}")


def notify_backup_result(db: Session, job, run):
    """Invia notifica email al termine di un backup se configurata."""
    if not job.notify_email:
        return
    if run.status.value == "success" and not job.notify_on_success:
        return
    if run.status.value == "failed" and not job.notify_on_failure:
        return

    status_label = {"success": "COMPLETATO", "failed": "FALLITO",
                    "partial": "PARZIALE", "running": "IN CORSO"}.get(run.status.value, run.status.value)

    duration = ""
    if run.finished_at and run.started_at:
        secs = int((run.finished_at - run.started_at).total_seconds())
        duration = f"{secs // 60}m {secs % 60}s"

    size_mb = f"{run.size_bytes / 1_048_576:.1f} MB" if run.size_bytes else "—"

    integrity_line = ""
    if run.integrity_verified is not None:
        integrity_line = f"Integrità verificata: {'Sì' if run.integrity_verified else 'NO - CHECKSUM NON CORRISPONDENTE'}\n"

    body = f"""Backup-All — Notifica automatica
═══════════════════════════════════════════════

Job:         {job.name}
Stato:       {status_label}
Inizio:      {run.started_at.strftime('%d/%m/%Y %H:%M:%S') if run.started_at else '—'}
Durata:      {duration}
Dimensione:  {size_mb}
{integrity_line}
{f"Errore: {run.error_message}" if run.error_message else ""}

Percorso backup: {run.backup_path or '—'}
"""
    subject = f"[Backup-All] {job.name} — {status_label}"
    send_email(db, job.notify_email, subject, body)
