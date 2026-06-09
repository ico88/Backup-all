"""Trasferimento backup verso QNAP via rsync o API QTS."""
import os
import subprocess
import requests
from app.crypto import decrypt


# ──────────────────────────────────────────────
# RSYNC (metodo principale)
# ──────────────────────────────────────────────

def rsync_to_qnap(dest_cfg, local_path: str, remote_subpath: str, log_fn=None) -> int:
    """
    Invia local_path al QNAP via rsync over SSH.
    dest_cfg: BackupDestination model instance.
    remote_subpath: es. "windows_gamma/2024-01-15"
    Ritorna byte trasferiti.
    """
    password = decrypt(dest_cfg.password_enc) if dest_cfg.password_enc else ""
    port = dest_cfg.port or 22
    user = dest_cfg.username
    host = dest_cfg.host
    remote_path = os.path.join(dest_cfg.base_path, remote_subpath).replace("\\", "/")

    if log_fn:
        log_fn(f"rsync → QNAP {host}:{remote_path}")

    env = os.environ.copy()
    if password:
        # Usa sshpass se disponibile, altrimenti chiave SSH
        env["SSHPASS"] = password
        ssh_cmd = f"sshpass -e ssh -p {port} -o StrictHostKeyChecking=no"
    else:
        ssh_cmd = f"ssh -p {port} -o StrictHostKeyChecking=no"

    cmd = [
        "rsync", "-avz", "--stats", "--mkpath",
        "-e", ssh_cmd,
        local_path + "/" if not local_path.endswith("/") else local_path,
        f"{user}@{host}:{remote_path}/",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"rsync verso QNAP fallito: {result.stderr}")

    if log_fn:
        log_fn(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)

    total_bytes = 0
    for line in result.stdout.splitlines():
        if "Total transferred file size:" in line:
            try:
                total_bytes = int(line.split(":")[1].strip().split()[0].replace(",", ""))
            except Exception:
                pass
    return total_bytes


# ──────────────────────────────────────────────
# QNAP QTS API (metodo alternativo)
# ──────────────────────────────────────────────

class QNAPClient:
    def __init__(self, dest_cfg):
        self.host = dest_cfg.host
        self.port = dest_cfg.port or 8080
        self.base_url = f"http://{self.host}:{self.port}/cgi-bin/filemanager"
        self.username = dest_cfg.username
        self.password = decrypt(dest_cfg.password_enc)
        self.sid = None

    def login(self):
        url = f"http://{self.host}:{self.port}/cgi-bin/authLogin.cgi"
        resp = requests.get(url, params={
            "user": self.username,
            "pwd": self.password,
        })
        resp.raise_for_status()
        # QTS risponde con XML
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        auth_passed = root.findtext(".//authPassed")
        if auth_passed != "1":
            raise RuntimeError("Autenticazione QNAP fallita")
        self.sid = root.findtext(".//authSid")

    def upload_file(self, local_file: str, remote_dir: str, log_fn=None):
        if not self.sid:
            self.login()
        url = f"{self.base_url}/utilRequest.cgi"
        filename = os.path.basename(local_file)
        if log_fn:
            log_fn(f"Upload {filename} su QNAP {remote_dir}...")
        with open(local_file, "rb") as f:
            resp = requests.post(url, params={
                "func": "upload",
                "type": "standard",
                "sid": self.sid,
                "dest_path": remote_dir,
                "overwrite": 1,
            }, files={"file": (filename, f)})
        resp.raise_for_status()

    def logout(self):
        if self.sid:
            requests.get(
                f"http://{self.host}:{self.port}/cgi-bin/authLogin.cgi",
                params={"logout": 1, "sid": self.sid}
            )


def apply_retention(dest_cfg, remote_base: str, retention_days: int, log_fn=None):
    """
    Rimuove backup più vecchi di retention_days giorni via SSH sul QNAP.
    remote_base: percorso base sul QNAP (es. /backup/windows_gamma)
    """
    if not retention_days:
        return
    password = decrypt(dest_cfg.password_enc) if dest_cfg.password_enc else ""
    port = dest_cfg.port or 22
    user = dest_cfg.username
    host = dest_cfg.host

    cmd_find = (
        f"find {remote_base} -maxdepth 1 -type d "
        f"-mtime +{retention_days} -exec rm -rf {{}} \\;"
    )
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        ssh_prefix = ["sshpass", "-e", "ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no"]
    else:
        ssh_prefix = ["ssh", "-p", str(port), "-o", "StrictHostKeyChecking=no"]

    result = subprocess.run(
        ssh_prefix + [f"{user}@{host}", cmd_find],
        capture_output=True, text=True, env=env
    )
    if log_fn:
        log_fn(f"Retention applicata: rimossi backup > {retention_days} giorni")
