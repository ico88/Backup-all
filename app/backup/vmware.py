"""Backup VM tramite API ESXi (pyvmomi): snapshot + export OVF."""
import os
import ssl
import time
import requests
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim
from app.crypto import decrypt


def _connect(host_cfg):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not host_cfg.ssl_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    si = SmartConnect(
        host=host_cfg.host,
        port=host_cfg.port,
        user=host_cfg.username,
        pwd=decrypt(host_cfg.password_enc),
        sslContext=context,
    )
    return si


def _find_vm(si, vm_name: str):
    content = si.RetrieveContent()
    container = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    for vm in container.view:
        if vm.name == vm_name:
            return vm
    return None


def _wait_task(task, log_fn=None):
    while task.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued):
        if log_fn:
            log_fn(f"Task {task.info.descriptionId}: {task.info.state}")
        time.sleep(5)
    if task.info.state != vim.TaskInfo.State.success:
        raise RuntimeError(f"Task fallito: {task.info.error.localizedMessage}")


def create_snapshot(host_cfg, vm_name: str, snapshot_name: str, log_fn=None):
    """Crea uno snapshot della VM."""
    si = _connect(host_cfg)
    try:
        vm = _find_vm(si, vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' non trovata su ESXi")
        task = vm.CreateSnapshot_Task(
            name=snapshot_name,
            description="Backup automatico",
            memory=False,
            quiesce=True,
        )
        _wait_task(task, log_fn)
        if log_fn:
            log_fn(f"Snapshot '{snapshot_name}' creato su '{vm_name}'")
    finally:
        Disconnect(si)


def remove_snapshot(host_cfg, vm_name: str, snapshot_name: str, log_fn=None):
    """Rimuove uno snapshot per nome."""
    si = _connect(host_cfg)
    try:
        vm = _find_vm(si, vm_name)
        if not vm:
            return
        snap_tree = vm.snapshot.rootSnapshotList if vm.snapshot else []

        def find_snap(tree, name):
            for s in tree:
                if s.name == name:
                    return s.snapshot
                found = find_snap(s.childSnapshotList, name)
                if found:
                    return found
            return None

        snap = find_snap(snap_tree, snapshot_name)
        if snap:
            task = snap.RemoveSnapshot_Task(removeChildren=False)
            _wait_task(task, log_fn)
            if log_fn:
                log_fn(f"Snapshot '{snapshot_name}' rimosso")
    finally:
        Disconnect(si)


def export_vm_ovf(host_cfg, vm_name: str, dest_dir: str, log_fn=None) -> str:
    """
    Esporta la VM come OVF nella directory dest_dir.
    Ritorna il percorso del file .ovf creato.
    Usa l'API HTTPS di ESXi per scaricare i VMDK.
    """
    si = _connect(host_cfg)
    try:
        vm = _find_vm(si, vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' non trovata su ESXi")

        content = si.RetrieveContent()
        lease = vm.ExportVm()

        # Attendi che il lease sia pronto
        while lease.state == vim.HttpNfcLease.State.initializing:
            time.sleep(2)
        if lease.state == vim.HttpNfcLease.State.error:
            raise RuntimeError(f"Export lease error: {lease.error.localizedMessage}")

        os.makedirs(dest_dir, exist_ok=True)
        base_url = f"https://{host_cfg.host}:{host_cfg.port}"
        session = requests.Session()
        session.verify = host_cfg.ssl_verify
        # Autentica la sessione HTTP con il cookie di vSphere
        session.cookies.update({"vmware_soap_session": si._stub.cookie.split('"')[1]})

        total_bytes = sum(i.size for i in lease.info.deviceUrl if i.size)
        downloaded = 0

        for device in lease.info.deviceUrl:
            filename = device.targetId or device.key.replace("/", "_")
            url = device.url.replace("*", host_cfg.host)
            if log_fn:
                log_fn(f"Download {filename} da ESXi...")
            dest_file = os.path.join(dest_dir, filename)
            with session.get(url, stream=True) as r:
                r.raise_for_status()
                with open(dest_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_bytes:
                            pct = int(downloaded * 100 / total_bytes)
                            lease.HttpNfcLeaseProgress(pct)

        lease.HttpNfcLeaseComplete()
        if log_fn:
            log_fn(f"Export VM '{vm_name}' completato in {dest_dir}")
        return dest_dir
    finally:
        Disconnect(si)


def export_vm_ovf_ovftool(host_cfg, vm_name: str, dest_dir: str, log_fn=None) -> str:
    """
    Esporta la VM come OVF usando ovftool — compatibile con ESXi Free License.
    Fallback quando le API pyvmomi (ExportVm) sono bloccate dalla licenza.
    """
    import shutil, subprocess, urllib.parse
    from app.crypto import decrypt

    candidates = [
        "/usr/lib/vmware-ovftool/ovftool",
        "/usr/bin/ovftool",
        "/opt/vmware/ovftool/ovftool",
    ]
    ovftool_bin = next((p for p in candidates if os.path.isfile(p)), None) or shutil.which("ovftool")
    if not ovftool_bin:
        raise RuntimeError(
            "ovftool non trovato. Installalo da https://developer.vmware.com/web/tool/ovf-tool "
            "oppure esegui: bash /opt/backup-all/install_ovftool.sh"
        )

    password = decrypt(host_cfg.password_enc)
    user_enc = urllib.parse.quote(host_cfg.username, safe="")
    pass_enc = urllib.parse.quote(password, safe="")
    vm_enc   = urllib.parse.quote(vm_name, safe="")
    port     = host_cfg.port or 443
    src_url  = f"vi://{user_enc}:{pass_enc}@{host_cfg.host}:{port}/{vm_enc}"

    os.makedirs(dest_dir, exist_ok=True)
    ovf_out = os.path.join(dest_dir, f"{vm_name}.ovf")

    cmd = [
        ovftool_bin,
        "--noSSLVerify",
        "--acceptAllEulas",
        "--skipManifestCheck",
        "--overwrite",
        src_url,
        ovf_out,
    ]
    if log_fn:
        safe = f"vi://{user_enc}:***@{host_cfg.host}:{port}/{vm_enc}"
        log_fn(f"ovftool export: {safe} → {ovf_out}")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        output_lines.append(line)
        if log_fn and line:
            log_fn(f"ovftool: {line}")
    proc.wait()
    if proc.returncode != 0:
        tail = "\n".join(output_lines[-20:])
        raise RuntimeError(f"ovftool export fallito (rc={proc.returncode}):\n{tail}")

    if log_fn:
        log_fn(f"Export VM '{vm_name}' completato via ovftool in {dest_dir}")
    return dest_dir


def list_vms(host_cfg) -> list[dict]:
    """Restituisce lista VM sull'host ESXi."""
    si = _connect(host_cfg)
    try:
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        result = []
        for vm in container.view:
            result.append({
                "name": vm.name,
                "power_state": str(vm.runtime.powerState),
                "guest_os": vm.config.guestFullName if vm.config else None,
                "num_cpu": vm.config.hardware.numCPU if vm.config else None,
                "memory_mb": vm.config.hardware.memoryMB if vm.config else None,
            })
        return result
    finally:
        Disconnect(si)
