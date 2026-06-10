"""Backup dati applicativi da server Linux via SSH/rsync."""
import json
import subprocess
import paramiko
from app.crypto import decrypt


def _get_ssh_client(server) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=server.ip_address,
        port=server.ssh_port or 22,
        username=server.username,
        timeout=30,
    )
    if server.ssh_key_path:
        connect_kwargs["key_filename"] = server.ssh_key_path
    else:
        connect_kwargs["password"] = decrypt(server.password_enc)
    client.connect(**connect_kwargs)
    return client


def backup_app_data(server, dest_dir: str, log_fn=None) -> int:
    """
    Copia via rsync i percorsi configurati in server.app_data_paths.
    Ritorna il totale byte trasferiti.
    """
    paths = json.loads(server.app_data_paths or "[]")
    if not paths:
        if log_fn:
            log_fn("Nessun percorso dati configurato per questo server", level="WARNING")
        return 0

    total_bytes = 0
    user = server.username
    host = server.ip_address
    port = server.ssh_port or 22

    for remote_path in paths:
        if log_fn:
            log_fn(f"rsync {user}@{host}:{remote_path} -> {dest_dir}")
        cmd = [
            "rsync", "-avz", "--stats",
            "-e", f"ssh -p {port} -o StrictHostKeyChecking=no",
        ]
        if server.ssh_key_path:
            cmd = [
                "rsync", "-avz", "--stats",
                "-e", f"ssh -p {port} -i {server.ssh_key_path} -o StrictHostKeyChecking=no",
            ]
        cmd += [f"{user}@{host}:{remote_path}", dest_dir]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"rsync fallito: {result.stderr}")

        # Parsing bytes trasferiti dall'output rsync
        for line in result.stdout.splitlines():
            if "Total transferred file size:" in line:
                try:
                    total_bytes += int(line.split(":")[1].strip().split()[0].replace(",", ""))
                except Exception:
                    pass
        if log_fn:
            log_fn(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)

    return total_bytes


def backup_database(server, dest_dir: str, log_fn=None) -> str:
    """
    Esegue pg_dump / mysqldump sul server remoto via SSH e scarica il dump.
    Ritorna il percorso locale del file dump.
    """
    import os
    db_type = (server.app_db_type or "").lower()
    db_name = server.app_db_name
    db_user = server.app_db_user
    db_pass = decrypt(server.app_db_password_enc) if server.app_db_password_enc else ""
    dump_file = f"/tmp/{db_name}_backup.sql"

    if db_type == "postgresql":
        remote_cmd = f"PGPASSWORD='{db_pass}' pg_dump -U {db_user} {db_name} > {dump_file}"
    elif db_type == "mysql":
        remote_cmd = f"mysqldump -u {db_user} -p'{db_pass}' {db_name} > {dump_file}"
    else:
        if log_fn:
            log_fn(f"Tipo DB '{db_type}' non supportato per dump automatico", level="WARNING")
        return ""

    client = _get_ssh_client(server)
    try:
        if log_fn:
            log_fn(f"Dump DB {db_type}:{db_name} sul server remoto...")
        _, stdout, stderr = client.exec_command(remote_cmd)
        stdout.channel.recv_exit_status()

        # Scarica il dump con SFTP
        sftp = client.open_sftp()
        local_dump = os.path.join(dest_dir, f"{db_name}_backup.sql")
        sftp.get(dump_file, local_dump)
        sftp.remove(dump_file)
        sftp.close()
        if log_fn:
            log_fn(f"DB dump salvato in {local_dump}")
        return local_dump
    finally:
        client.close()
