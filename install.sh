#!/usr/bin/env bash
# ==============================================================================
#  Backup-All — Installer per Ubuntu Server
#  CRI Catania
# ==============================================================================
set -euo pipefail

# ── Colori ────────────────────────────────────────────
RED='\033[0;31m'; BOLD='\033[1m'; DIM='\033[2m'
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "${GREEN}${BOLD}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}${BOLD}  →${NC}  $*"; }
warn() { echo -e "${YELLOW}${BOLD}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}${BOLD}  ✗${NC}  $*"; exit 1; }
step() { echo -e "\n${BOLD}${CYAN}[$1]${NC} ${BOLD}$2${NC}"; }

# ── Banner ────────────────────────────────────────────
clear
echo -e "${RED}${BOLD}"
echo "  ██████╗  █████╗  ██████╗██╗  ██╗██╗   ██╗██████╗       █████╗ ██╗     ██╗     "
echo "  ██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██║   ██║██╔══██╗     ██╔══██╗██║     ██║     "
echo "  ██████╔╝███████║██║     █████╔╝ ██║   ██║██████╔╝     ███████║██║     ██║     "
echo "  ██╔══██╗██╔══██║██║     ██╔═██╗ ██║   ██║██╔═══╝      ██╔══██║██║     ██║     "
echo "  ██████╔╝██║  ██║╚██████╗██║  ██╗╚██████╔╝██║          ██║  ██║███████╗███████╗"
echo "  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝          ╚═╝  ╚═╝╚══════╝╚══════╝"
echo -e "${NC}"
echo -e "  ${DIM}Soluzione di backup unificata — CRI Catania${NC}"
echo -e "  ${DIM}────────────────────────────────────────────${NC}\n"

# ── Root check ────────────────────────────────────────
[[ $EUID -ne 0 ]] && err "Esegui lo script come root: sudo bash install.sh"

# ── Variabili di default ──────────────────────────────
INSTALL_DIR="/opt/backup-all"
SERVICE_USER="backupall"
SERVICE_NAME="backup-all"
REPO_URL="https://github.com/ico88/Backup-all.git"
REPO_BRANCH="claude/loving-keller-d6978c"
PYTHON_MIN="3.11"

# ══════════════════════════════════════════════════════
#  FASE 1 — Raccolta parametri interattiva
# ══════════════════════════════════════════════════════
step "1/6" "Configurazione"

# Porta
while true; do
    echo -e "  ${BOLD}Porta su cui avviare Backup-All${NC}"
    echo -e "  ${DIM}(scegli una porta libera, es. 8080, 8443, 9000, 9500)${NC}"
    read -rp "  Porta: " APP_PORT
    if [[ "$APP_PORT" =~ ^[0-9]+$ ]] && (( APP_PORT >= 1024 && APP_PORT <= 65535 )); then
        # Verifica che la porta non sia già in uso
        if ss -tlnp 2>/dev/null | grep -q ":${APP_PORT} " || \
           lsof -iTCP:${APP_PORT} -sTCP:LISTEN -t 2>/dev/null | grep -q .; then
            warn "La porta ${APP_PORT} è già in uso. Scegline un'altra."
        else
            ok "Porta selezionata: ${APP_PORT}"
            break
        fi
    else
        warn "Inserisci un numero di porta valido (1024–65535)."
    fi
done

# Directory di installazione
echo ""
echo -e "  ${BOLD}Directory di installazione${NC} ${DIM}[default: ${INSTALL_DIR}]${NC}"
read -rp "  Percorso (invio per default): " CUSTOM_DIR
[[ -n "$CUSTOM_DIR" ]] && INSTALL_DIR="$CUSTOM_DIR"
ok "Installazione in: ${INSTALL_DIR}"

# Riepilogo
echo ""
echo -e "  ${BOLD}╔══════════════════════════════════════╗${NC}"
echo -e "  ${BOLD}║       Riepilogo configurazione       ║${NC}"
echo -e "  ${BOLD}╠══════════════════════════════════════╣${NC}"
echo -e "  ${BOLD}║${NC}  Porta:       ${GREEN}${BOLD}${APP_PORT}${NC}"
echo -e "  ${BOLD}║${NC}  Cartella:    ${INSTALL_DIR}"
echo -e "  ${BOLD}║${NC}  Utente svc:  ${SERVICE_USER}"
echo -e "  ${BOLD}║${NC}  Systemd:     ${SERVICE_NAME}.service"
echo -e "  ${BOLD}╚══════════════════════════════════════╝${NC}"
echo ""
read -rp "  Procedere con l'installazione? [S/n]: " CONFIRM
[[ "${CONFIRM,,}" == "n" ]] && { echo "  Installazione annullata."; exit 0; }

# ══════════════════════════════════════════════════════
#  FASE 2 — Dipendenze di sistema
# ══════════════════════════════════════════════════════
step "2/6" "Installazione dipendenze di sistema"

info "Aggiornamento lista pacchetti..."
apt-get update -qq

PKGS=(python3 python3-pip python3-venv git rsync sshpass curl)
for pkg in "${PKGS[@]}"; do
    if dpkg -s "$pkg" &>/dev/null; then
        ok "$pkg già installato"
    else
        info "Installazione $pkg..."
        apt-get install -y -qq "$pkg"
        ok "$pkg installato"
    fi
done

# Verifica versione Python
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if (( PY_MAJOR < 3 || (PY_MAJOR == 3 && PY_MINOR < 11) )); then
    warn "Python ${PY_VER} rilevato. Backup-All richiede Python 3.11+."
    info "Installazione python3.11 da deadsnakes PPA..."
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
    PYTHON_BIN="python3.11"
    ok "Python 3.11 installato"
else
    PYTHON_BIN="python3"
    ok "Python ${PY_VER} — versione compatibile"
fi

# ══════════════════════════════════════════════════════
#  FASE 3 — Utente di sistema e cartella
# ══════════════════════════════════════════════════════
step "3/6" "Creazione utente e directory"

if id "$SERVICE_USER" &>/dev/null; then
    ok "Utente '${SERVICE_USER}' già esistente"
else
    useradd --system --shell /usr/sbin/nologin --home-dir "${INSTALL_DIR}" \
            --comment "Backup-All service user" "$SERVICE_USER"
    ok "Utente di sistema '${SERVICE_USER}' creato"
fi

# Backup installazione precedente se esiste
if [[ -d "$INSTALL_DIR" ]]; then
    BACKUP_DIR="${INSTALL_DIR}.bak.$(date +%Y%m%d_%H%M%S)"
    warn "Trovata installazione precedente. Backup in: ${BACKUP_DIR}"
    mv "$INSTALL_DIR" "$BACKUP_DIR"
fi

mkdir -p "$INSTALL_DIR"
ok "Directory ${INSTALL_DIR} creata"

# ══════════════════════════════════════════════════════
#  FASE 4 — Clone repo e venv
# ══════════════════════════════════════════════════════
step "4/6" "Download applicazione"

info "Clone repository..."
git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR" 2>&1 | \
    sed 's/^/    /'
ok "Repository clonato"

info "Creazione virtual environment Python..."
"$PYTHON_BIN" -m venv "${INSTALL_DIR}/.venv"
ok "Virtualenv creato in ${INSTALL_DIR}/.venv"

info "Installazione dipendenze Python..."
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip -q
# Installa senza pyvmomi se la build fallisce (richiede build tools)
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q 2>&1 || {
    warn "Alcune dipendenze opzionali non sono state installate (es. pyvmomi)."
    warn "Installazione core senza pyvmomi..."
    "${INSTALL_DIR}/.venv/bin/pip" install \
        fastapi uvicorn sqlalchemy alembic paramiko pywinrm requests \
        apscheduler jinja2 python-multipart aiofiles cryptography \
        smbprotocol python-dotenv bcrypt itsdangerous -q
}
ok "Dipendenze Python installate"

# Crea cartella static se non esiste
mkdir -p "${INSTALL_DIR}/static"

# Permessi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"
ok "Permessi impostati"

# ══════════════════════════════════════════════════════
#  FASE 5 — File di configurazione e systemd
# ══════════════════════════════════════════════════════
step "5/6" "Configurazione servizio systemd"

# File .env
cat > "${INSTALL_DIR}/.env" << EOF
PORT=${APP_PORT}
DEV=false
EOF
chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
ok "File .env creato"

# Unit file systemd
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Backup-All — CRI Catania
Documentation=https://github.com/ico88/Backup-all
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PORT=${APP_PORT}"
EnvironmentFile=-${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/run.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

# Limiti di sicurezza
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${INSTALL_DIR}
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF

ok "Unit file /etc/systemd/system/${SERVICE_NAME}.service creato"

# Reload e avvio
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl start "${SERVICE_NAME}"

# Attendi che si avvii
sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "Servizio avviato con successo"
else
    warn "Il servizio non sembra attivo. Controlla con: journalctl -u ${SERVICE_NAME} -n 50"
fi

# ══════════════════════════════════════════════════════
#  FASE 6 — Firewall (opzionale)
# ══════════════════════════════════════════════════════
step "6/6" "Firewall"

if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
    read -rp "  UFW attivo. Aprire la porta ${APP_PORT}/tcp? [S/n]: " UFW_OPEN
    if [[ "${UFW_OPEN,,}" != "n" ]]; then
        ufw allow "${APP_PORT}/tcp" comment "Backup-All"
        ok "Porta ${APP_PORT}/tcp aperta nel firewall"
    else
        warn "Ricordati di aprire la porta ${APP_PORT} manualmente se necessario"
    fi
else
    info "UFW non attivo o non installato — nessuna regola firewall aggiunta"
fi

# ══════════════════════════════════════════════════════
#  Riepilogo finale
# ══════════════════════════════════════════════════════
SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════╗"
echo "  ║          Installazione completata!  ✓            ║"
echo "  ╚══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  ${BOLD}Accedi da browser:${NC}"
echo -e "  ${CYAN}${BOLD}  → http://${SERVER_IP}:${APP_PORT}${NC}"
echo ""
echo -e "  ${BOLD}Credenziali iniziali:${NC}"
echo -e "     Username:  ${BOLD}admin${NC}"
echo -e "     Password:  ${BOLD}changeme${NC}  ${RED}← cambiala subito!${NC}"
echo ""
echo -e "  ${BOLD}Comandi utili:${NC}"
echo -e "  ${DIM}  Stato servizio:${NC}   systemctl status ${SERVICE_NAME}"
echo -e "  ${DIM}  Log in tempo reale:${NC} journalctl -u ${SERVICE_NAME} -f"
echo -e "  ${DIM}  Riavvio:${NC}           systemctl restart ${SERVICE_NAME}"
echo -e "  ${DIM}  Arresto:${NC}           systemctl stop ${SERVICE_NAME}"
echo -e "  ${DIM}  File log DB:${NC}       ${INSTALL_DIR}/backup.db"
echo ""
echo -e "  ${DIM}────────────────────────────────────────────────────${NC}"
echo -e "  ${DIM}Installato in: ${INSTALL_DIR}${NC}"
echo -e "  ${DIM}Utente servizio: ${SERVICE_USER}${NC}"
echo -e "  ${DIM}Systemd unit: /etc/systemd/system/${SERVICE_NAME}.service${NC}"
echo ""
