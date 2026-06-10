"""Backup dati applicativi da server Windows via WinRM."""
import json
import os
import winrm
from app.crypto import decrypt


def _get_session(server) -> winrm.Session:
    return winrm.Session(
        target=f"http://{server.ip_address}:{server.winrm_port or 5985}/wsman",
        auth=(server.username, decrypt(server.password_enc)),
        transport="ntlm",
    )


def backup_app_data(server, dest_dir: str, log_fn=None) -> int:
    """
    Copia i file da Windows usando robocopy via WinRM verso una share SMB
    raggiungibile dal server Windows, oppure via WinRM download diretto.
    Usa PowerShell + Compress-Archive per creare uno zip dei dati, poi
    lo scarica con WinRM.
    """
    paths = json.loads(server.app_data_paths or "[]")
    if not paths:
        if log_fn:
            log_fn("Nessun percorso dati configurato", level="WARNING")
        return 0

    session = _get_session(server)
    total_bytes = 0

    for remote_path in paths:
        safe_name = remote_path.replace(":", "").replace("\\", "_").replace("/", "_")
        zip_remote = f"C:\\Windows\\Temp\\backup_{safe_name}.zip"

        ps_cmd = f"""
        $source = '{remote_path}'
        $dest = '{zip_remote}'
        if (Test-Path $source) {{
            Compress-Archive -Path $source -DestinationPath $dest -Force
            Write-Output "OK:$dest"
        }} else {{
            Write-Output "NOTFOUND:$source"
        }}
        """
        if log_fn:
            log_fn(f"Compressione {remote_path} su Windows...")
        result = session.run_ps(ps_cmd)
        output = result.std_out.decode().strip()

        if result.status_code != 0 or "NOTFOUND" in output:
            if log_fn:
                log_fn(f"Percorso non trovato o errore: {output}", level="WARNING")
            continue

        # Scarica lo zip via WinRM (lettura chunk base64)
        ps_download = f"""
        $bytes = [System.IO.File]::ReadAllBytes('{zip_remote}')
        [Convert]::ToBase64String($bytes)
        """
        if log_fn:
            log_fn(f"Download {zip_remote} dal server Windows...")
        dl_result = session.run_ps(ps_download)
        b64_data = dl_result.std_out.strip()
        import base64
        zip_data = base64.b64decode(b64_data)
        local_zip = os.path.join(dest_dir, f"backup_{safe_name}.zip")
        with open(local_zip, "wb") as f:
            f.write(zip_data)
        total_bytes += len(zip_data)

        # Rimuovi zip temporaneo da Windows
        session.run_ps(f"Remove-Item '{zip_remote}' -Force")
        if log_fn:
            log_fn(f"Salvato {local_zip} ({len(zip_data):,} bytes)")

    return total_bytes


def backup_mssql(server, dest_dir: str, log_fn=None) -> str:
    """
    Esegue backup SQL Server via T-SQL (BACKUP DATABASE TO DISK).
    Richiede che il path di destinazione sia accessibile dal servizio SQL Server.
    """
    db_name = server.app_db_name
    if not db_name:
        return ""

    backup_path = f"C:\\Windows\\Temp\\{db_name}_backup.bak"
    ps_cmd = f"""
    $conn = New-Object System.Data.SqlClient.SqlConnection
    $conn.ConnectionString = "Server=localhost;Database=master;Integrated Security=True;"
    $conn.Open()
    $cmd = $conn.CreateCommand()
    $cmd.CommandText = "BACKUP DATABASE [{db_name}] TO DISK = N'{backup_path}' WITH FORMAT, STATS = 10"
    $cmd.CommandTimeout = 3600
    $cmd.ExecuteNonQuery()
    $conn.Close()
    Write-Output "BACKUP_OK:{backup_path}"
    """
    session = _get_session(server)
    if log_fn:
        log_fn(f"Backup SQL Server database '{db_name}'...")
    result = session.run_ps(ps_cmd)
    output = result.std_out.decode().strip()

    if result.status_code != 0 or "BACKUP_OK" not in output:
        raise RuntimeError(f"Backup MSSQL fallito: {result.std_err.decode()}")

    # Scarica il .bak
    ps_download = f"""
    $bytes = [System.IO.File]::ReadAllBytes('{backup_path}')
    [Convert]::ToBase64String($bytes)
    """
    if log_fn:
        log_fn(f"Download {backup_path}...")
    dl_result = session.run_ps(ps_download)
    import base64
    bak_data = base64.b64decode(dl_result.std_out.strip())
    local_bak = os.path.join(dest_dir, f"{db_name}_backup.bak")
    with open(local_bak, "wb") as f:
        f.write(bak_data)
    session.run_ps(f"Remove-Item '{backup_path}' -Force")
    if log_fn:
        log_fn(f"Salvato {local_bak} ({len(bak_data):,} bytes)")
    return local_bak
