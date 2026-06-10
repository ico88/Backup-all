"""Replica VM tra host ESXi via ovftool."""
import subprocess
import shutil
import time
import socket
import logging
from datetime import datetime, timezone

from app.crypto import decrypt

log = logging.getLogger("backup-all.replication")

OVFTOOL_PATHS = [
    "/usr/lib/vmware-ovftool/ovftool",
    "/usr/bin/ovftool",
    "/opt/vmware/ovftool/ovftool",
]


def _find_ovftool() -> str:
    for p in OVFTOOL_PATHS:
        if shutil.which(p) or __import__("os").path.isfile(p):
            return p
    found = shutil.which("ovftool")
    if found:
        return found
    raise RuntimeError(
        "ovftool non trovato. Installalo da: "
        "https://developer.vmware.com/web/tool/ovf-tool "
        "oppure esegui: bash /opt/backup-all/install_ovftool.sh"
    )


def _vi_url(host, vm_name: str, decrypt_fn=None) -> str:
    """Costruisce URL vi:// per ovftool."""
    password = decrypt(host.password_enc) if host.password_enc else ""
    import urllib.parse
    user_enc = urllib.parse.quote(host.username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    return f"vi://{user_enc}:{pass_enc}@{host.host}:{host.port or 443}/{vm_name}"


def sync_vm(job, log_fn=None) -> str:
    """
    Copia VM-A su VM-B via ovftool diretto ESXi→ESXi.
    Restituisce output log come stringa.
    """
    ovftool = _find_ovftool()

    src_host = job.source_host
    dst_host = job.target_host

    src_url = _vi_url(src_host, job.source_vm_name)
    # Per la destinazione passiamo solo l'host (senza VM name nel path)
    dst_password = decrypt(dst_host.password_enc) if dst_host.password_enc else ""
    import urllib.parse
    dst_user_enc = urllib.parse.quote(dst_host.username, safe="")
    dst_pass_enc = urllib.parse.quote(dst_password, safe="")
    dst_url = f"vi://{dst_user_enc}:{dst_pass_enc}@{dst_host.host}:{dst_host.port or 443}/"
    if job.target_datastore:
        dst_url += f"?ds=[{job.target_datastore}]"

    cmd = [
        ovftool,
        "--noSSLVerify",
        "--acceptAllEulas",
        "--powerOffSource",           # snapshot online, poi spegne temporaneamente per export
        "--overwrite",                # sovrascrive VM-B se esiste già
        "--skipManifestCheck",
        f"--name={job.target_vm_name}",
        "--X:waitForIp",
    ]
    if job.target_datastore:
        cmd.append(f"--datastore={job.target_datastore}")
    cmd += [src_url, dst_url]

    if log_fn:
        log_fn(f"Avvio replica: {job.source_vm_name} → {job.target_vm_name}")
        log_fn(f"Sorgente ESXi: {src_host.host}  |  Destinazione ESXi: {dst_host.host}")

    log.info("ovftool cmd (passwords masked): %s", " ".join(
        p if "vi://" not in p else p.split("@")[-1] for p in cmd
    ))

    output_lines = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in proc.stdout:
        line = line.rstrip()
        output_lines.append(line)
        if log_fn:
            log_fn(f"ovftool: {line}")

    proc.wait()
    output = "\n".join(output_lines)

    if proc.returncode != 0:
        raise RuntimeError(f"ovftool terminato con codice {proc.returncode}:\n{output[-1000:]}")

    if log_fn:
        log_fn(f"Replica completata: VM '{job.target_vm_name}' su {dst_host.host} aggiornata e in standby.")

    return output


def check_heartbeat(job) -> bool:
    """
    Controlla se VM-A è ancora attiva.
    Prima verifica powerState via pyvmomi, poi opzionalmente pinga l'IP.
    Restituisce True se alive, False se irraggiungibile.
    """
    try:
        from pyVmomi import vim
        from app.backup.vmware import _connect
        si = _connect(job.source_host)
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        for vm in container.view:
            if vm.name == job.source_vm_name:
                state = vm.runtime.powerState
                container.Destroy()
                if state != vim.VirtualMachinePowerState.poweredOn:
                    log.warning("Heartbeat: VM %s powerState=%s", job.source_vm_name, state)
                    return False
                # Se ha IP configurato, pinga anche quello
                if job.source_vm_ip:
                    return _ping(job.source_vm_ip)
                return True
        container.Destroy()
        log.warning("Heartbeat: VM %s non trovata su host %s", job.source_vm_name, job.source_host.host)
        return False
    except Exception as e:
        log.error("Heartbeat check fallito: %s", e)
        return False


def _ping(ip: str, timeout: int = 3) -> bool:
    """Ping TCP su porta 22 o 3389 come proxy di liveness."""
    for port in (22, 3389, 80, 443):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except (socket.timeout, ConnectionRefusedError):
            return True   # porta chiusa ma host raggiungibile
        except OSError:
            continue
    return False


def promote_standby(job, log_fn=None):
    """
    Promuove VM-B a primaria: accende VM-B.
    Non tocca VM-A (potrebbe essere già down/corrotta).
    """
    try:
        from pyVmomi import vim
        from app.backup.vmware import _connect
        si = _connect(job.target_host)
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        for vm in container.view:
            if vm.name == job.target_vm_name:
                container.Destroy()
                if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
                    if log_fn:
                        log_fn(f"Avvio VM standby '{job.target_vm_name}' su {job.target_host.host}...")
                    task = vm.PowerOn()
                    _wait_task(task, log_fn)
                    if log_fn:
                        log_fn(f"VM '{job.target_vm_name}' avviata. Failover completato.")
                else:
                    if log_fn:
                        log_fn(f"VM '{job.target_vm_name}' era già accesa.")
                return
        container.Destroy()
        raise RuntimeError(f"VM standby '{job.target_vm_name}' non trovata su {job.target_host.host}")
    except Exception as e:
        raise RuntimeError(f"Promozione fallita: {e}")


def _wait_task(task, log_fn=None, timeout=120):
    from pyVmomi import vim
    start = time.time()
    while time.time() - start < timeout:
        if task.info.state == vim.TaskInfo.State.success:
            return
        if task.info.state == vim.TaskInfo.State.error:
            raise RuntimeError(str(task.info.error))
        time.sleep(2)
    raise RuntimeError("Task VMware timeout")
