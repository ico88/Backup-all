"""Backup dati applicativi da server Windows via WinRM."""
import base64
import json
import os
import time
import uuid
import winrm
from app.crypto import decrypt


def _ps_quote(value: str) -> str:
    """Quote a value for a single-quoted PowerShell string."""
    return "'" + (value or "").replace("'", "''") + "'"


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


def backup_system_wbadmin(server, dest_cfg, timestamp: str, log_fn=None) -> None:
    """
    Backup completo bare-metal Windows via wbadmin direttamente su share SMB del QNAP.
    Il server Windows deve poter raggiungere la share SMB: \\\\<host>\\<smb_share>.
    """
    smb_share = dest_cfg.smb_share
    if not smb_share:
        raise ValueError("smb_share non configurato nella destinazione")

    smb_user = dest_cfg.username or ""
    from app.crypto import decrypt as _decrypt
    smb_password = _decrypt(dest_cfg.password_enc) if dest_cfg.password_enc else ""

    unc_base = f"\\\\{dest_cfg.host}\\{smb_share}"
    backup_target = f"{unc_base}\\{server.name}\\{timestamp}"

    task_id = uuid.uuid4().hex[:12]
    task_name = f"BackupAll_wbadmin_{task_id}"
    script_path = f"C:\\Windows\\Temp\\{task_name}.ps1"
    script_b64_path = f"C:\\Windows\\Temp\\{task_name}.b64"
    log_path = f"C:\\Windows\\Temp\\{task_name}.log"
    server_password = decrypt(server.password_enc)

    backup_script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$uncBase = {_ps_quote(unc_base)}
$backupTarget = {_ps_quote(backup_target)}
$smbUser = {_ps_quote(smb_user)}
$smbPassword = {_ps_quote(smb_password)}
$logPath = {_ps_quote(log_path)}

function Write-BackupLog([string]$message) {{
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $message"
    Add-Content -Path $logPath -Value $line
    Write-Output $line
}}

Write-BackupLog "Script avviato come $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-BackupLog "Target backup: $backupTarget"
try {{
    if ($smbUser) {{
        Write-BackupLog "Pulizia connessioni SMB esistenti verso $uncBase"
        & cmd.exe /c "net use ""$uncBase"" /delete /yes >nul 2>nul"
        $candidateUsers = @($smbUser)
        if ($smbUser -notmatch '[\\@]') {{
            $candidateUsers += "{dest_cfg.host}\$smbUser"
        }}
        $connected = $false
        $lastNetUseOutput = ''
        $lastNetUseCode = 0
        foreach ($candidateUser in $candidateUsers) {{
            Write-BackupLog "Tentativo connessione SMB con utente $candidateUser"
            $netUseArgs = @('use', $uncBase, $smbPassword, "/user:$candidateUser", '/persistent:no')
            $oldPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $netUseOutput = & net.exe @netUseArgs 2>&1
            $lastNetUseCode = $LASTEXITCODE
            $ErrorActionPreference = $oldPreference
            $lastNetUseOutput = "utente=$candidateUser; output=$netUseOutput"
            if ($lastNetUseCode -eq 0) {{
                $connected = $true
                Write-BackupLog "Connessione SMB riuscita con utente $candidateUser"
                break
            }}
            Write-BackupLog "Connessione SMB fallita con codice $lastNetUseCode: $netUseOutput"
            & cmd.exe /c "net use ""$uncBase"" /delete /yes >nul 2>nul"
        }}
        if (-not $connected) {{
            throw "Connessione SMB fallita ($lastNetUseCode): $lastNetUseOutput"
        }}
    }}

    Write-BackupLog "Creazione cartella destinazione $backupTarget"
    New-Item -ItemType Directory -Path $backupTarget -Force | Out-Null
    Write-BackupLog "Avvio wbadmin"
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & wbadmin start backup -backupTarget:$backupTarget -include:C: -allCritical -quiet 2>&1
    $rc = $LASTEXITCODE
    $ErrorActionPreference = $oldPreference
    Write-BackupLog "$output"
    if ($rc -ne 0) {{
        throw "WBADMIN_FAIL:$rc"
    }}
    Write-BackupLog 'WBADMIN_OK'
    exit 0
}} catch {{
    Write-BackupLog "ERRORE: $($_.Exception.Message)"
    exit 1
}} finally {{
    if ($smbUser) {{
        & cmd.exe /c "net use ""$uncBase"" /delete /yes >nul 2>nul"
    }}
}}
"""
    script_b64 = base64.b64encode(backup_script.encode("utf-8")).decode("ascii")

    if log_fn:
        log_fn(f"Avvio backup sistema Windows verso {backup_target} ...")

    session = _get_session(server)
    init_upload_cmd = f"""
$ErrorActionPreference = 'Stop'
Remove-Item {_ps_quote(script_b64_path)} -Force -ErrorAction SilentlyContinue
Remove-Item {_ps_quote(script_path)} -Force -ErrorAction SilentlyContinue
New-Item -ItemType File -Path {_ps_quote(script_b64_path)} -Force | Out-Null
"""
    init_result = session.run_ps(init_upload_cmd)
    if init_result.status_code != 0:
        raise RuntimeError(
            "wbadmin fallito: preparazione script remoto fallita: "
            + init_result.std_err.decode(errors="replace")
        )

    for offset in range(0, len(script_b64), 500):
        chunk = script_b64[offset:offset + 500]
        append_cmd = f"""
$ErrorActionPreference = 'Stop'
Add-Content -Path {_ps_quote(script_b64_path)} -Value {_ps_quote(chunk)} -NoNewline
"""
        append_result = session.run_ps(append_cmd)
        if append_result.status_code != 0:
            raise RuntimeError(
                "wbadmin fallito: caricamento script remoto fallito: "
                + append_result.std_err.decode(errors="replace")
            )

    setup_cmd = f"""
$ErrorActionPreference = 'Stop'
$scriptPath = {_ps_quote(script_path)}
$scriptB64Path = {_ps_quote(script_b64_path)}
$taskName = {_ps_quote(task_name)}
$taskUser = {_ps_quote(server.username)}
$taskPassword = {_ps_quote(server_password)}
$scriptBytes = [Convert]::FromBase64String((Get-Content $scriptB64Path -Raw))
[System.IO.File]::WriteAllBytes($scriptPath, $scriptBytes)
$scriptText = Get-Content $scriptPath -Raw
Set-Content -Path $scriptPath -Value $scriptText -Encoding UTF8
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 24) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User $taskUser -Password $taskPassword -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "TASK_STARTED:$taskName"
"""
    setup_result = session.run_ps(setup_cmd)
    setup_output = setup_result.std_out.decode(errors="replace")
    setup_stderr = setup_result.std_err.decode(errors="replace")
    if setup_result.status_code != 0 or "TASK_STARTED:" not in setup_output:
        raise RuntimeError(f"wbadmin fallito: avvio task schedulato fallito: {setup_stderr or setup_output}")

    last_log_len = 0
    deadline = time.time() + 24 * 60 * 60
    final_result = None
    while time.time() < deadline:
        time.sleep(15)
        poll_cmd = f"""
$task = Get-ScheduledTask -TaskName {_ps_quote(task_name)}
$info = Get-ScheduledTaskInfo -TaskName {_ps_quote(task_name)}
$log = ''
if (Test-Path {_ps_quote(log_path)}) {{
    $log = Get-Content {_ps_quote(log_path)} -Raw
}}
Write-Output "STATE=$($task.State)"
Write-Output "LAST_RESULT=$($info.LastTaskResult)"
Write-Output "LOG_BEGIN"
Write-Output $log
Write-Output "LOG_END"
"""
        poll_result = session.run_ps(poll_cmd)
        poll_output = poll_result.std_out.decode(errors="replace")
        if "LOG_BEGIN" in poll_output and "LOG_END" in poll_output:
            log_text = poll_output.split("LOG_BEGIN", 1)[1].split("LOG_END", 1)[0].strip()
            if log_fn and len(log_text) > last_log_len:
                new_text = log_text[last_log_len:].strip()
                for line in new_text.splitlines()[-20:]:
                    if line.strip():
                        log_fn(f"wbadmin: {line}")
                last_log_len = len(log_text)
        if "STATE=Running" not in poll_output:
            final_result = poll_output
            break

    cleanup_cmd = f"""
Unregister-ScheduledTask -TaskName {_ps_quote(task_name)} -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item {_ps_quote(script_path)} -Force -ErrorAction SilentlyContinue
Remove-Item {_ps_quote(script_b64_path)} -Force -ErrorAction SilentlyContinue
Remove-Item {_ps_quote(log_path)} -Force -ErrorAction SilentlyContinue
"""
    session.run_ps(cleanup_cmd)

    if not final_result:
        raise RuntimeError("wbadmin fallito: timeout del task schedulato")

    output = final_result

    if "WBADMIN_OK" not in output:
        err_detail = output[-1000:]
        raise RuntimeError(f"wbadmin fallito: {err_detail}")
