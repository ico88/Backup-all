# Backup-All — CRI Catania

Sistema automatizzato di backup per VM VMware ESXi con UI web di configurazione.

## Architettura

- **Backend**: Python 3.11 + FastAPI
- **Database**: SQLite (via SQLAlchemy) — sostituibile con PostgreSQL/MySQL
- **Scheduler**: APScheduler (cron)
- **Backup VM**: pyvmomi → ESXi snapshot + export OVF
- **Backup dati Windows** (Gamma/TeamSystem): WinRM + PowerShell
- **Backup dati Linux** (Abulafia): SSH + rsync + pg_dump
- **Destinazione**: QNAP via rsync-over-SSH o API QTS

## Installazione

```bash
pip install -r requirements.txt
python run.py
```

Apri il browser su `http://localhost:8000`

## Primo avvio — wizard configurazione

1. **Aggiungi Server** → `/wizard/server`
   - Configura Windows Server (Gamma) e Debian (Abulafia)
   - Inserisci credenziali WinRM / SSH
   - Collega all'host ESXi per backup OVF

2. **Aggiungi QNAP** → `/wizard/destination`
   - IP del NAS, credenziali SSH, percorso base

3. **Crea Job** → `/wizard/job`
   - Scegli server, destinazione, tipo backup
   - Configura schedule cron (o usa i preset)

## Prerequisiti

### Server Windows (Gamma)
```powershell
winrm quickconfig -y
winrm set winrm/config/service/auth '@{Basic="true"}'
```

### Server Debian (Abulafia)
- SSH abilitato con utente con accesso ai dati

### QNAP
- Abilitare SSH: `Pannello di Controllo > Terminale & SNMP > Abilita SSH`
- Creare utente dedicato backup con accesso alla share

### ESXi
- Utente con ruolo Administrator o almeno permessi snapshot + datastore

## Struttura backup sul QNAP

```
/share/Backup/cri-catania/
  windows-gamma/
    2024-01-15_02-00-00/
      vm_export/          <- OVF + VMDK
      app_data/           <- file Gamma + .bak SQL Server
  debian-abulafia/
    2024-01-15_02-00-00/
      vm_export/
      app_data/           <- dati + dump PostgreSQL
```
