"""Backup VM tramite API ESXi (pyvmomi): snapshot + export OVF."""
import os
import ssl
import time
import urllib.parse
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


def export_vm_datastore(host_cfg, vm_name: str, dest_dir: str, log_fn=None) -> str:
    """
    Scarica i file della VM direttamente dall'HTTPS datastore di ESXi.
    Funziona su ESXi Free License — non usa API NFC né ExportVm.
    Scarica: .vmx, .vmdk (descrittore), -flat.vmdk (dati disco), .nvram
    """
    si = _connect(host_cfg)
    try:
        vm = _find_vm(si, vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' non trovata su ESXi")

        # Ricava datastore e percorso cartella dalla config VM
        # vmPathName è tipo "[datastore1] vm-name/vm-name.vmx"
        vm_path = vm.config.files.vmPathName
        ds_name = vm_path.split("]")[0].lstrip("[")
        vm_folder = vm_path.split("] ")[1].rsplit("/", 1)[0]

        if log_fn:
            log_fn(f"Datastore: {ds_name}, cartella VM: {vm_folder}")

        # Elenca i file nella cartella VM via API (lettura — consentita su Free)
        content = si.RetrieveContent()
        search_spec = vim.host.DatastoreBrowser.SearchSpec()
        search_spec.matchPattern = ["*.vmx", "*.vmdk", "*.nvram", "*.vmsd"]

        # Trova il datastore
        ds_obj = None
        for ds in content.rootFolder.childEntity[0].datastore:
            if ds.name == ds_name:
                ds_obj = ds
                break
        if not ds_obj:
            raise ValueError(f"Datastore '{ds_name}' non trovato")

        browser = ds_obj.browser
        ds_path = f"[{ds_name}] {vm_folder}"
        task = browser.SearchDatastore_Task(datastorePath=ds_path, searchSpec=search_spec)
        _wait_task(task, None)
        results = task.info.result

        if not results or not results.file:
            raise ValueError(f"Nessun file trovato in {ds_path}")

        os.makedirs(dest_dir, exist_ok=True)
        from app.crypto import decrypt
        password = decrypt(host_cfg.password_enc)
        port = host_cfg.port or 443
        base_url = f"https://{host_cfg.host}:{port}"

        session = requests.Session()
        session.verify = host_cfg.ssl_verify
        # Autentica la sessione con cookie vSphere (lettura file — consentita su Free)
        cookie_val = si._stub.cookie
        if '"' in cookie_val:
            cookie_val = cookie_val.split('"')[1]
        session.cookies.set("vmware_soap_session", cookie_val)

        total_files = len(results.file)
        for idx, f in enumerate(results.file, 1):
            fname = f.path
            # Salta i -flat.vmdk se il descrittore .vmdk è già nella lista
            # (i -flat.vmdk vengono inclusi automaticamente come file separati)
            encoded_path = urllib.parse.quote(f"{vm_folder}/{fname}")
            url = f"{base_url}/folder/{encoded_path}?dcPath=ha-datacenter&dsName={urllib.parse.quote(ds_name)}"
            dest_file = os.path.join(dest_dir, fname)

            if log_fn:
                size_mb = round(f.fileSize / 1024 / 1024, 1) if hasattr(f, 'fileSize') and f.fileSize else "?"
                log_fn(f"Download [{idx}/{total_files}] {fname} ({size_mb} MB)...")

            with session.get(url, stream=True, timeout=3600) as r:
                r.raise_for_status()
                with open(dest_file, "wb") as out:
                    for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                        out.write(chunk)

        if log_fn:
            log_fn(f"Download VM '{vm_name}' completato in {dest_dir}")
        return dest_dir
    finally:
        Disconnect(si)


def stream_vm_to_remote(host_cfg, vm_name: str, dest_cfg, remote_path: str, log_fn=None):
    """
    Streaming diretto ESXi → QNAP via SSH: zero spazio disco locale.
    Per ogni file della VM: curl (ESXi HTTPS) | ssh (QNAP) cat > file
    """
    import shutil, subprocess
    from app.crypto import decrypt as _dec

    si = _connect(host_cfg)
    try:
        vm = _find_vm(si, vm_name)
        if not vm:
            raise ValueError(f"VM '{vm_name}' non trovata su ESXi")

        vm_path   = vm.config.files.vmPathName
        ds_name   = vm_path.split("]")[0].lstrip("[")
        vm_folder = vm_path.split("] ")[1].rsplit("/", 1)[0]

        # Lista file via API (read-only, consentito su Free)
        content = si.RetrieveContent()
        search_spec = vim.host.DatastoreBrowser.SearchSpec()
        search_spec.matchPattern = ["*.vmx", "*.vmdk", "*.nvram", "*.vmsd"]
        ds_obj = next((d for d in content.rootFolder.childEntity[0].datastore
                       if d.name == ds_name), None)
        if not ds_obj:
            raise ValueError(f"Datastore '{ds_name}' non trovato")

        task = ds_obj.browser.SearchDatastore_Task(
            datastorePath=f"[{ds_name}] {vm_folder}",
            searchSpec=search_spec,
        )
        _wait_task(task, None)
        files = task.info.result.file if task.info.result else []
        if not files:
            raise ValueError(f"Nessun file VM trovato in [{ds_name}] {vm_folder}")

        # Cookie sessione ESXi per autenticare curl
        cookie_raw = si._stub.cookie
        cookie_val = cookie_raw.split('"')[1] if '"' in cookie_raw else cookie_raw
        esxi_port  = host_cfg.port or 443

        # Credenziali QNAP
        qnap_pass = _dec(dest_cfg.password_enc) if dest_cfg.password_enc else ""
        qnap_port = dest_cfg.port or 22
        qnap_user = dest_cfg.username
        qnap_host = dest_cfg.host

        has_sshpass = shutil.which("sshpass") is not None
        if qnap_pass and not has_sshpass:
            raise RuntimeError("sshpass non installato — esegui: sudo apt install sshpass")

        ssh_env = {"SSHPASS": qnap_pass} if qnap_pass else {}

        # Crea directory remota
        ssh_opts = [
            "-p", str(qnap_port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "LogLevel=ERROR",        # sopprime warning post-quantum e simili
            "-o", "BatchMode=yes",
        ]
        ssh_pre = ["sshpass", "-e", "ssh"] if qnap_pass else ["ssh"]
        ssh_pre += ssh_opts + [f"{qnap_user}@{qnap_host}"]
        import os as _os
        _os_env = _os.environ.copy()
        _os_env.update(ssh_env)

        # Crea la directory remota e verifica
        mkdir_res = subprocess.run(
            ssh_pre + [f"mkdir -p '{remote_path}'"],
            env=_os_env, capture_output=True, text=True
        )
        if mkdir_res.returncode != 0:
            raise RuntimeError(
                f"Impossibile creare la directory sul QNAP '{remote_path}': "
                f"{mkdir_res.stderr.strip() or mkdir_res.stdout.strip()}"
            )

        total = len(files)
        for idx, f in enumerate(files, 1):
            fname = f.path
            enc_path = urllib.parse.quote(f"{vm_folder}/{fname}")
            esxi_url = (f"https://{host_cfg.host}:{esxi_port}/folder/{enc_path}"
                        f"?dcPath=ha-datacenter&dsName={urllib.parse.quote(ds_name)}")
            remote_file = f"{remote_path}/{fname}"

            size_info = ""
            if hasattr(f, "fileSize") and f.fileSize:
                size_info = f" ({round(f.fileSize/1024/1024/1024, 2)} GB)"
            if log_fn:
                log_fn(f"Stream [{idx}/{total}] {fname}{size_info} → QNAP...")

            # curl -k  → sshpass ssh "cat > remote_file"
            # Usa variabile bash per evitare problemi di quoting con spazi nel path
            remote_file_escaped = remote_file.replace("'", "'\\''")
            ssh_cmd = f"cat > '{remote_file_escaped}'"

            curl_cmd = [
                "curl", "-sk", "--retry", "2",
                "-o", "-",
                "-b", f"vmware_soap_session={cookie_val}",
                esxi_url,
            ]
            ssh_write = ssh_pre + [ssh_cmd]

            curl = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            ssh  = subprocess.Popen(ssh_write, stdin=curl.stdout,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=_os_env)
            curl.stdout.close()
            _, ssh_err = ssh.communicate()
            _, curl_err = curl.communicate()
            curl.wait()

            if curl.returncode != 0:
                raise RuntimeError(
                    f"curl fallito per {fname} (rc={curl.returncode}): "
                    f"{curl_err.decode(errors='replace')[:400]}"
                )
            if ssh.returncode != 0:
                err_text = ssh_err.decode(errors='replace').strip()
                raise RuntimeError(
                    f"SSH write fallito per {fname} (rc={ssh.returncode}): {err_text[:400]}"
                )

        if log_fn:
            log_fn(f"VM '{vm_name}' trasferita su QNAP in {remote_path}")
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
