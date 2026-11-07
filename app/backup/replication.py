"""Replica VM tra host ESXi via ovftool (full) e CBT pyvmomi (incrementale)."""
import json
import os
import socket
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import logging
from datetime import datetime, timezone

import requests
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

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


# ── Helpers pyvmomi per CBT ──────────────────────────────────────────────────

def _esxi_connect(host_cfg):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not host_cfg.ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return SmartConnect(
        host=host_cfg.host, port=host_cfg.port or 443,
        user=host_cfg.username, pwd=decrypt(host_cfg.password_enc),
        sslContext=ctx,
    )


def _find_vm_by_name(si, name):
    container = si.RetrieveContent().viewManager.CreateContainerView(
        si.RetrieveContent().rootFolder, [vim.VirtualMachine], True
    )
    return next((v for v in container.view if v.name == name), None)


def _wait_pyvmomi_task(task, log_fn=None):
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        time.sleep(3)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"Task ESXi fallito: {task.info.error.localizedMessage}")


def _find_snap_ref(vm, snap_name):
    def _search(tree):
        for s in tree:
            if s.name == snap_name:
                return s.snapshot
            found = _search(s.childSnapshotList)
            if found:
                return found
        return None
    return _search(vm.snapshot.rootSnapshotList) if vm.snapshot else None


def _esxi_http_session(host_cfg, si):
    session = requests.Session()
    session.verify = host_cfg.ssl_verify
    cookie_raw = si._stub.cookie
    cookie_val = cookie_raw.split('"')[1] if '"' in cookie_raw else cookie_raw
    session.cookies.set("vmware_soap_session", cookie_val)
    return session


def _flat_url(host_cfg, rel_path, ds_name):
    port = host_cfg.port or 443
    flat = rel_path.replace(".vmdk", "-flat.vmdk")
    return (f"https://{host_cfg.host}:{port}/folder/{urllib.parse.quote(flat)}"
            f"?dcPath=ha-datacenter&dsName={urllib.parse.quote(ds_name)}")


def setup_repl_cbt_baseline(src_host, src_vm_name, dst_host, dst_vm_name, log_fn=None) -> dict:
    """
    Da chiamare dopo il primo sync completo via ovftool.
    Abilita CBT sulla sorgente, crea snapshot baseline, legge changeId iniziali
    e mappa i VMDK della destinazione. Ritorna disk_states JSON-serializable.
    """
    snap_name = f"repl_base_{int(time.time())}"
    si_src = _esxi_connect(src_host)
    try:
        vm = _find_vm_by_name(si_src, src_vm_name)
        if not vm:
            raise ValueError(f"VM '{src_vm_name}' non trovata su {src_host.host}")

        # Abilita CBT
        if not vm.config.changeTrackingEnabled:
            spec = vim.vm.ConfigSpec()
            spec.changeTrackingEnabled = True
            _wait_pyvmomi_task(vm.ReconfigVM_Task(spec=spec), log_fn)
            _emit(log_fn, "CBT abilitato sulla VM sorgente")

        # Raccoglie info dischi sorgente
        src_disks = {}
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                fn = dev.backing.fileName
                src_disks[dev.key] = {
                    "src_filename": fn,
                    "size": dev.capacityInBytes,
                }

        # Snapshot baseline
        _emit(log_fn, "Snapshot baseline CBT in corso...")
        _wait_pyvmomi_task(vm.CreateSnapshot_Task(
            name=snap_name, description="CBT baseline replica",
            memory=False, quiesce=False,
        ), log_fn)

        snap_ref = _find_snap_ref(vm, snap_name)
        for dev in snap_ref.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk) and dev.key in src_disks:
                cid = getattr(dev.backing, "changeId", None)
                if cid:
                    src_disks[dev.key]["change_id"] = cid

        # Mappa VMDK destinazione (stesso ordine di disco)
        si_dst = _esxi_connect(dst_host)
        try:
            vm_dst = _find_vm_by_name(si_dst, dst_vm_name)
            if vm_dst:
                dst_devs = [d for d in vm_dst.config.hardware.device
                            if isinstance(d, vim.vm.device.VirtualDisk)]
                src_keys = sorted(src_disks.keys())
                for i, dev in enumerate(dst_devs):
                    if i < len(src_keys):
                        src_disks[src_keys[i]]["dst_filename"] = dev.backing.fileName
        finally:
            Disconnect(si_dst)

        disk_states = {
            str(k): {
                "change_id": v["change_id"],
                "src_filename": v["src_filename"],
                "dst_filename": v.get("dst_filename", ""),
                "size": v["size"],
            }
            for k, v in src_disks.items()
            if "change_id" in v
        }
        _emit(log_fn, f"CBT baseline pronto: {len(disk_states)} disco/i — sync successivi saranno incrementali")
        return disk_states

    finally:
        try:
            vm2 = _find_vm_by_name(si_src, src_vm_name)
            if vm2:
                snap = _find_snap_ref(vm2, snap_name)
                if snap:
                    snap.RemoveSnapshot_Task(removeChildren=False)
        except Exception:
            pass
        Disconnect(si_src)


def sync_vm_cbt_incremental(job, prev_disk_states: dict, log_fn=None) -> dict:
    """
    Replica incrementale CBT: trasferisce solo i blocchi modificati.
    Ritorna i nuovi disk_states da salvare in DB.
    """
    src_host = job.source_host
    dst_host = job.target_host
    snap_name = f"repl_incr_{int(time.time())}"
    total_bytes = 0

    si_src = _esxi_connect(src_host)
    try:
        vm_src = _find_vm_by_name(si_src, job.source_vm_name)
        if not vm_src:
            raise ValueError(f"VM '{job.source_vm_name}' non trovata su {src_host.host}")

        _emit(log_fn, "Creazione snapshot incrementale sulla VM sorgente...")
        _wait_pyvmomi_task(vm_src.CreateSnapshot_Task(
            name=snap_name, description="Replica incrementale CBT",
            memory=False, quiesce=True,
        ), log_fn)
        snap_ref = _find_snap_ref(vm_src, snap_name)

        # Nuovi changeId dallo snapshot
        new_cids = {}
        for dev in snap_ref.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                new_cids[dev.key] = getattr(dev.backing, "changeId", None)

        src_session = _esxi_http_session(src_host, si_src)

        si_dst = _esxi_connect(dst_host)
        dst_session = _esxi_http_session(dst_host, si_dst)

        try:
            # Spegni VM standby se accesa (necessario per scrivere sul VMDK)
            vm_dst = _find_vm_by_name(si_dst, job.target_vm_name)
            if vm_dst and vm_dst.runtime.powerState != vim.VirtualMachinePowerState.poweredOff:
                _emit(log_fn, "Spegnimento VM standby per applicare aggiornamenti...")
                _wait_pyvmomi_task(vm_dst.PowerOffVM_Task(), log_fn)

            result_states = {}

            for key_str, state in prev_disk_states.items():
                disk_key = int(key_str)
                prev_cid = state["change_id"]
                src_fn = state["src_filename"]   # [ds] path/vm.vmdk
                dst_fn = state["dst_filename"]
                disk_size = state["size"]

                src_ds  = src_fn.split("]")[0].lstrip("[").strip()
                src_rel = src_fn.split("] ")[1].strip()
                dst_ds  = dst_fn.split("]")[0].lstrip("[").strip()
                dst_rel = dst_fn.split("] ")[1].strip()

                _emit(log_fn, f"Disco {os.path.basename(src_rel)}: query blocchi modificati...")

                try:
                    info = vm_src.QueryChangedDiskAreas(
                        snapshot=snap_ref, deviceKey=disk_key,
                        startOffset=0, changeId=prev_cid,
                    )
                    extents = info.changedArea
                except Exception as e:
                    _emit(log_fn, f"CBT query fallita ({e}): trasferimento completo del disco", "WARNING")
                    info = vm_src.QueryChangedDiskAreas(
                        snapshot=snap_ref, deviceKey=disk_key,
                        startOffset=0, changeId="*",
                    )
                    extents = info.changedArea

                changed_mb = round(sum(e.length for e in extents) / 1024 / 1024, 1)
                _emit(log_fn, f"  {len(extents)} extent modificati — {changed_mb} MB da trasferire")

                if not extents:
                    _emit(log_fn, "  Nessuna modifica — disco aggiornato")
                    result_states[key_str] = {**state, "change_id": new_cids.get(disk_key, prev_cid)}
                    continue

                src_flat_url = _flat_url(src_host, src_rel, src_ds)
                dst_flat_url = _flat_url(dst_host, dst_rel, dst_ds)

                for i, extent in enumerate(extents):
                    start  = extent.start
                    length = extent.length

                    # Leggi extent dalla sorgente (disco congelato dallo snapshot)
                    r = src_session.get(
                        src_flat_url,
                        headers={"Range": f"bytes={start}-{start+length-1}"},
                        stream=True, timeout=300,
                    )
                    if r.status_code not in (200, 206):
                        raise RuntimeError(f"Download extent fallito: HTTP {r.status_code}")
                    data = b"".join(r.iter_content(chunk_size=1024 * 1024))

                    # Scrivi sulla destinazione
                    put_r = dst_session.put(
                        dst_flat_url, data=data,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Range": f"bytes {start}-{start+len(data)-1}/{disk_size}",
                        },
                        timeout=300,
                    )
                    if put_r.status_code not in (200, 201, 204):
                        raise RuntimeError(f"Upload extent fallito: HTTP {put_r.status_code}")

                    total_bytes += len(data)
                    if (i + 1) % 20 == 0 or i == len(extents) - 1:
                        _emit(log_fn, f"  {i+1}/{len(extents)} extent — {round(total_bytes/1024/1024, 1)} MB totali")

                result_states[key_str] = {**state, "change_id": new_cids.get(disk_key, prev_cid)}

        finally:
            Disconnect(si_dst)

        _emit(log_fn, f"Replica CBT incrementale completata: {round(total_bytes/1024/1024, 1)} MB trasferiti")
        return result_states

    finally:
        try:
            vm2 = _find_vm_by_name(si_src, job.source_vm_name)
            if vm2:
                snap = _find_snap_ref(vm2, snap_name)
                if snap:
                    snap.RemoveSnapshot_Task(removeChildren=False)
        except Exception:
            pass
        Disconnect(si_src)


def _get_vm_networks(host_cfg) -> list[str]:
    """Legge le reti OVF della VM sorgente (nomi rete nei device)."""
    try:
        import ssl
        from pyVim.connect import SmartConnect, Disconnect
        from pyVmomi import vim
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not host_cfg.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        si = SmartConnect(host=host_cfg.host, port=host_cfg.port or 443,
                          user=host_cfg.username, pwd=decrypt(host_cfg.password_enc),
                          sslContext=ctx)
        try:
            content = si.RetrieveContent()
            nets = set()
            for dc in content.rootFolder.childEntity:
                for net in getattr(dc, "network", []):
                    nets.add(net.name)
            return sorted(nets)
        finally:
            Disconnect(si)
    except Exception:
        return []


def _get_host_networks(host_cfg) -> list[str]:
    """Legge i port group disponibili sull'host ESXi destinazione."""
    try:
        import ssl
        from pyVim.connect import SmartConnect, Disconnect
        from pyVmomi import vim
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not host_cfg.ssl_verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        si = SmartConnect(host=host_cfg.host, port=host_cfg.port or 443,
                          user=host_cfg.username, pwd=decrypt(host_cfg.password_enc),
                          sslContext=ctx)
        try:
            content = si.RetrieveContent()
            nets = set()
            for dc in content.rootFolder.childEntity:
                for net in getattr(dc, "network", []):
                    nets.add(net.name)
            return sorted(nets)
        finally:
            Disconnect(si)
    except Exception:
        return []


def _resolve_network_mapping(src_host, dst_host, vm_ovf_networks: list[str],
                              target_network_override: str | None,
                              log_fn=None) -> dict[str, str]:
    """
    Risolve automaticamente il mapping reti OVF → port group destinazione.
    Ritorna {ovf_net_name: dst_portgroup_name}.
    """
    if target_network_override:
        return {n: target_network_override for n in vm_ovf_networks}

    dst_nets = _get_host_networks(dst_host)
    if not dst_nets:
        _emit(log_fn, "Impossibile leggere reti ESXi destinazione — mapping automatico saltato", "WARNING")
        return {}

    mapping = {}
    for ovf_net in vm_ovf_networks:
        if ovf_net in dst_nets:
            mapping[ovf_net] = ovf_net
            _emit(log_fn, f"Rete '{ovf_net}' → '{ovf_net}' (corrispondenza esatta)")
        else:
            # Usa il primo port group disponibile come fallback
            fallback = dst_nets[0]
            mapping[ovf_net] = fallback
            _emit(log_fn, f"Rete '{ovf_net}' non trovata su destinazione → fallback '{fallback}' "
                          f"(disponibili: {', '.join(dst_nets)})", "WARNING")
    return mapping


def _vi_url(host, vm_name: str, decrypt_fn=None) -> str:
    """Costruisce URL vi:// per ovftool."""
    password = decrypt(host.password_enc) if host.password_enc else ""
    import urllib.parse
    user_enc = urllib.parse.quote(host.username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    return f"vi://{user_enc}:{pass_enc}@{host.host}:{host.port or 443}/{vm_name}"


def sync_vm(job, db_session=None, log_fn=None) -> str:
    """
    Replica VM-A su VM-B.
    - Prima volta: ovftool completo + setup CBT baseline
    - Volte successive: CBT incrementale (solo blocchi modificati)
    """
    from app.models import VmReplCbtState

    # Controlla se esiste uno stato CBT per questo job
    cbt_state = None
    if db_session:
        cbt_state = db_session.query(VmReplCbtState).filter_by(job_id=job.id).first()

    if cbt_state and cbt_state.disk_states and cbt_state.disk_states != "{}":
        prev = json.loads(cbt_state.disk_states)
        _emit(log_fn, f"Replica incrementale CBT — {len(prev)} disco/i tracciati (solo blocchi modificati)")
        new_states = sync_vm_cbt_incremental(job, prev, log_fn)
        if db_session:
            cbt_state.disk_states = json.dumps(new_states)
            db_session.commit()
        return "Replica incrementale CBT completata"

    # Prima volta o CBT non disponibile: sync completo via ovftool
    result = _sync_vm_full(job, log_fn)

    # Dopo il sync completo, imposta il baseline CBT
    if db_session:
        try:
            _emit(log_fn, "Inizializzazione CBT per sync incrementali futuri...")
            disk_states = setup_repl_cbt_baseline(
                job.source_host, job.source_vm_name,
                job.target_host, job.target_vm_name,
                log_fn,
            )
            if disk_states:
                if cbt_state:
                    cbt_state.disk_states = json.dumps(disk_states)
                else:
                    cbt_state = VmReplCbtState(
                        job_id=job.id,
                        disk_states=json.dumps(disk_states),
                    )
                    db_session.add(cbt_state)
                db_session.commit()
                _emit(log_fn, "CBT attivato — il prossimo sync trasferirà solo le modifiche")
        except Exception as e:
            _emit(log_fn, f"Setup CBT non riuscito ({e}) — i sync resteranno completi", "WARNING")

    return result


def _sync_vm_full(job, log_fn=None) -> str:
    """
    Sync completo via ovftool (usato per il primo sync o se CBT non disponibile).
    """
    started = time.monotonic()
    _emit(log_fn, "Preparazione replica VM-to-VM (sync completo)")
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

    # Risolvi mapping reti automaticamente
    _emit(log_fn, "Rilevamento reti VM sorgente e port group destinazione...")
    src_nets = _get_host_networks(src_host)
    net_mapping = _resolve_network_mapping(
        src_host, dst_host,
        src_nets or ["VM Network"],
        getattr(job, "target_network", None),
        log_fn,
    )

    cmd = [
        ovftool,
        "--noSSLVerify",
        "--acceptAllEulas",
        "--overwrite",
        "--skipManifestCheck",
        f"--name={job.target_vm_name}",
        "--X:waitForIp",
    ]
    for ovf_net, dst_net in net_mapping.items():
        cmd.append(f"--net:{ovf_net}={dst_net}")
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
