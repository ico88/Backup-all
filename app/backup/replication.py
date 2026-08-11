"""Replica VM tra host ESXi via ovftool."""
import subprocess
import shutil
import time
import socket
import logging
import os
import threading
from datetime import datetime, timezone

from app.crypto import decrypt

log = logging.getLogger("backup-all.replication")

OVFTOOL_PATHS = [
    "/usr/lib/vmware-ovftool/ovftool",
    "/usr/bin/ovftool",
    "/opt/vmware/ovftool/ovftool",
]

_ACTIVE_PROCS: dict[int, subprocess.Popen] = {}
_ACTIVE_PROCS_LOCK = threading.Lock()


def _register_proc(job_id: int, proc: subprocess.Popen):
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS[job_id] = proc


def _unregister_proc(job_id: int):
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.pop(job_id, None)


def cancel_sync(job_id: int) -> bool:
    with _ACTIVE_PROCS_LOCK:
        proc = _ACTIVE_PROCS.get(job_id)
    if not proc or proc.poll() is not None:
        return False
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return True


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


def _emit(log_fn, message: str, level: str = "INFO"):
    prefix = f"[{level}] "
    if log_fn:
        log_fn(prefix + message)
    if level == "ERROR":
        log.error(message)
    elif level == "WARNING":
        log.warning(message)
    else:
        log.info(message)


def _mask_vi_arg(arg: str) -> str:
    if not arg.startswith("vi://"):
        return arg
    try:
        scheme, rest = arg.split("://", 1)
        if "@" not in rest:
            return arg
        _, host_part = rest.rsplit("@", 1)
        return f"{scheme}://***:***@{host_part}"
    except Exception:
        return "vi://***"


def _safe_cmd(cmd: list[str]) -> str:
    return " ".join(_mask_vi_arg(p) for p in cmd)


def _classify_ovftool_error(output: str) -> list[str]:
    text = output.lower()
    hints = []
    checks = [
        (("no network mapping specified", "ovf networks", "target networks"), "Mappatura rete mancante: indica a ovftool su quale port group dell'host destinazione collegare la rete della VM."),
        (("unsupported hardware family", "vmx-"), "Compatibilita hardware VM: l'host ESXi destinazione e' troppo vecchio per la virtual hardware version della VM sorgente."),
        (("operating system identifier", "is not supported on the selected host"), "Compatibilita guest OS: l'host destinazione non riconosce pienamente il tipo sistema operativo della VM."),
        (("license", "restrictedversion", "current license"), "Licenza/versione ESXi: l'host potrebbe bloccare operazioni richieste da ovftool."),
        (("permission", "no permission", "access denied", "login failed", "authentication"), "Credenziali/permessi: verifica utente ESXi, password e privilegi su VM/datastore."),
        (("unable to connect", "connection refused", "timed out", "could not resolve"), "Rete/DNS: verifica raggiungibilità host ESXi, porta 443 e nome/IP configurati."),
        (("datastore", "no space", "insufficient disk", "not enough space"), "Datastore: verifica nome datastore destinazione e spazio disponibile."),
        (("already exists", "overwrite"), "VM destinazione: esiste già o non può essere sovrascritta; verifica nome VM standby e permessi."),
        (("ssl", "certificate", "thumbprint"), "SSL/certificato: verifica accesso HTTPS agli host ESXi o opzione noSSLVerify."),
        (("power", "powered", "snapshot", "quiesce"), "Stato VM/snapshot: verifica power state, snapshot esistenti e VMware Tools se richiesti."),
    ]
    for needles, hint in checks:
        if any(n in text for n in needles):
            hints.append(hint)
    return hints


def _tail(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return "... output precedente omesso ...\n" + text[-limit:]


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
    started = time.monotonic()
    _emit(log_fn, "Preparazione replica VM-to-VM")
    ovftool = _find_ovftool()

    src_host = job.source_host
    dst_host = job.target_host

    _emit(log_fn, f"ovftool trovato: {ovftool}")
    _emit(log_fn, f"Job: {job.name} (id={job.id})")
    _emit(log_fn, f"Sorgente: host={src_host.host}:{src_host.port or 443}, VM='{job.source_vm_name}'")
    _emit(log_fn, f"Destinazione: host={dst_host.host}:{dst_host.port or 443}, VM='{job.target_vm_name}'")
    _emit(log_fn, f"Datastore destinazione: {job.target_datastore or 'predefinito ESXi'}")

    if src_host.id == dst_host.id and job.source_vm_name == job.target_vm_name:
        raise RuntimeError("Configurazione non valida: sorgente e destinazione puntano alla stessa VM sullo stesso host.")

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
        "--maxVirtualHardwareVersion=10",
        f"--name={job.target_vm_name}",
        "--X:waitForIp",
    ]
    if job.target_network:
        # Mappa ogni rete OVF della VM sul port group di destinazione configurato
        cmd.append(f"--net:VM Network={job.target_network}")
    if job.target_datastore:
        cmd.append(f"--datastore={job.target_datastore}")
    cmd += [src_url, dst_url]

    _emit(log_fn, f"Comando ovftool: {_safe_cmd(cmd)}")
    _emit(log_fn, "Avvio processo ovftool; da qui in poi riporto stdout/stderr in tempo reale.")

    output_lines = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ.copy(), "LC_ALL": "C", "LANG": "C", "LC_CTYPE": "C"},
        )
    except FileNotFoundError:
        raise RuntimeError(f"ovftool non eseguibile o non trovato: {ovftool}")
    except PermissionError:
        raise RuntimeError(f"Permesso negato eseguendo ovftool: {ovftool}")

    assert proc.stdout is not None
    _register_proc(job.id, proc)
    try:
        last_progress_at = time.monotonic()
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            output_lines.append(line)
            _emit(log_fn, f"ovftool: {line}")
            last_progress_at = time.monotonic()

        if time.monotonic() - last_progress_at > 60:
            _emit(log_fn, "ovftool non ha prodotto output recente prima della chiusura.", "WARNING")

        proc.wait()
    finally:
        _unregister_proc(job.id)
    output = "\n".join(output_lines)
    duration = int(time.monotonic() - started)
    _emit(log_fn, f"ovftool terminato con codice {proc.returncode} dopo {duration}s")

    if proc.returncode is not None and proc.returncode < 0:
        _emit(log_fn, "Replica interrotta manualmente: processo ovftool terminato.", "WARNING")
        raise RuntimeError("Replica interrotta manualmente.")

    if proc.returncode != 0:
        hints = _classify_ovftool_error(output)
        if hints:
            _emit(log_fn, "Possibili cause rilevate:", "ERROR")
            for hint in hints:
                _emit(log_fn, f"- {hint}", "ERROR")
        else:
            _emit(log_fn, "Nessuna causa riconosciuta automaticamente. Controlla la coda output ovftool.", "ERROR")
        _emit(log_fn, "Coda output ovftool:", "ERROR")
        for line in _tail(output).splitlines():
            _emit(log_fn, line, "ERROR")
        raise RuntimeError(
            f"Replica VM fallita: ovftool rc={proc.returncode}. "
            f"Vedi log run per comando, output e possibili cause."
        )

    _emit(log_fn, f"Replica completata: VM '{job.target_vm_name}' su {dst_host.host} aggiornata e in standby.")

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
