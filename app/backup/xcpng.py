"""Backup VM XCP-ng via XAPI (xmlrpc.client) + streaming HTTP export."""
import ssl
import subprocess
import urllib.parse
import xmlrpc.client
from app.crypto import decrypt


def _session(host_cfg):
    """Apre sessione XAPI, ritorna (proxy, session_ref)."""
    ctx = ssl.create_default_context()
    if not host_cfg.ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    transport = xmlrpc.client.SafeTransport(context=ctx)
    proxy = xmlrpc.client.ServerProxy(
        f"https://{host_cfg.host}:{host_cfg.port or 443}",
        transport=transport, allow_none=True
    )
    password = decrypt(host_cfg.password_enc)
    r = proxy.session.login_with_password(host_cfg.username, password, "1.0", "backup-all")
    if r.get("Status") != "Success":
        raise RuntimeError(f"Login XAPI fallito: {r.get('ErrorDescription', r)}")
    return proxy, r["Value"]


def _logout(proxy, session_ref):
    try:
        proxy.session.logout(session_ref)
    except Exception:
        pass


def list_vms(host_cfg) -> list[dict]:
    """Lista VM sull'host XCP-ng (escluse template e control domain)."""
    proxy, sid = _session(host_cfg)
    try:
        r = proxy.VM.get_all_records(sid)
        if r.get("Status") != "Success":
            raise RuntimeError(f"get_all_records fallito: {r}")
        result = []
        for ref, rec in r["Value"].items():
            if rec.get("is_a_template") or rec.get("is_control_domain"):
                continue
            power = rec.get("power_state", "unknown")
            result.append({
                "name": rec["name_label"],
                "uuid": rec["uuid"],
                "power_state": power,
                "vcpus": rec.get("VCPUs_max", "?"),
                "memory_mb": int(rec.get("memory_static_max", 0)) // (1024 * 1024),
            })
        return sorted(result, key=lambda x: x["name"])
    finally:
        _logout(proxy, sid)


def stream_vm_to_remote(host_cfg, vm_name: str, dest_cfg, remote_path: str, log_fn=None):
    """
    Snapshot VM → export XVA via HTTPS → pipe SSH su QNAP.
    Zero spazio disco locale. Compatibile con XCP-ng licenza gratuita.
    """
    import os, shutil
    from app.crypto import decrypt as _dec

    proxy, sid = _session(host_cfg)
    snapshot_ref = None
    try:
        # Trova la VM per nome
        r = proxy.VM.get_by_name_label(sid, vm_name)
        if r.get("Status") != "Success" or not r["Value"]:
            raise ValueError(f"VM '{vm_name}' non trovata su XCP-ng {host_cfg.host}")
        vm_ref = r["Value"][0]

        # Crea snapshot
        snap_name = f"backup-tmp"
        if log_fn:
            log_fn(f"Creazione snapshot '{snap_name}'...")
        r = proxy.VM.snapshot(sid, vm_ref, snap_name)
        if r.get("Status") != "Success":
            raise RuntimeError(f"Snapshot fallito: {r.get('ErrorDescription', r)}")
        snapshot_ref = r["Value"]

        # Recupera UUID snapshot per l'URL export
        r = proxy.VM.get_uuid(sid, snapshot_ref)
        snap_uuid = r["Value"]
        if log_fn:
            log_fn(f"Snapshot creato (uuid={snap_uuid[:8]}...), avvio export XVA...")

        # URL export XVA — usa session_id come query param (no Basic Auth necessaria)
        esxi_port = host_cfg.port or 443
        export_url = (
            f"https://{host_cfg.host}:{esxi_port}/export"
            f"?session_id={urllib.parse.quote(sid)}"
            f"&uuid={snap_uuid}"
            f"&use_compression=zstd"
        )

        # Credenziali QNAP
        qnap_pass = _dec(dest_cfg.password_enc) if dest_cfg.password_enc else ""
        qnap_port = dest_cfg.port or 22
        qnap_user = dest_cfg.username
        qnap_host = dest_cfg.host

        has_sshpass = shutil.which("sshpass") is not None
        if qnap_pass and not has_sshpass:
            raise RuntimeError("sshpass non installato — esegui: sudo apt install sshpass")

        ssh_env = os.environ.copy()
        if qnap_pass:
            ssh_env["SSHPASS"] = qnap_pass

        ssh_opts = ["-p", str(qnap_port), "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR"]
        ssh_pre = (["sshpass", "-e", "ssh"] if qnap_pass else ["ssh"]) + ssh_opts + [f"{qnap_user}@{qnap_host}"]

        # Crea directory remota
        mkdir_res = subprocess.run(ssh_pre + [f"mkdir -p '{remote_path}'"],
                                   env=ssh_env, capture_output=True, text=True)
        if mkdir_res.returncode != 0:
            err = mkdir_res.stderr.strip() or mkdir_res.stdout.strip()
            raise RuntimeError(
                f"Impossibile creare '{remote_path}' sul QNAP: {err}\n"
                f"→ Verifica che '{dest_cfg.base_path}' esista e che '{dest_cfg.username}' "
                f"abbia permessi di scrittura."
            )

        # Stream: curl (XCP-ng HTTPS) → ssh cat > file QNAP
        remote_file = f"{remote_path}/{vm_name}.xva"
        if log_fn:
            log_fn(f"Stream XVA → QNAP {qnap_host}:{remote_file}...")

        curl_cmd = ["curl", "-sk", "--retry", "2", "-o", "-", export_url]
        ssh_write = ssh_pre + [f"cat > '{remote_file}'"]

        curl = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ssh  = subprocess.Popen(ssh_write, stdin=curl.stdout,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ssh_env)
        curl.stdout.close()
        _, ssh_err = ssh.communicate()
        _, curl_err = curl.communicate()
        curl.wait()

        if curl.returncode != 0:
            raise RuntimeError(f"curl fallito (rc={curl.returncode}): {curl_err.decode(errors='replace')[:400]}")
        if ssh.returncode != 0:
            raise RuntimeError(f"SSH write fallito (rc={ssh.returncode}): {ssh_err.decode(errors='replace')[:400]}")

        if log_fn:
            log_fn(f"VM '{vm_name}' esportata su QNAP come {vm_name}.xva")

    finally:
        # Rimuovi snapshot
        if snapshot_ref:
            try:
                if log_fn:
                    log_fn("Rimozione snapshot temporaneo...")
                proxy.VM.destroy(sid, snapshot_ref)
            except Exception as e:
                if log_fn:
                    log_fn(f"Attenzione: impossibile rimuovere snapshot: {e}", "WARNING")
        _logout(proxy, sid)
