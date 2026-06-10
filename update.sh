#!/usr/bin/env bash
# ==============================================================================
#  Backup-All — Script di aggiornamento (aggiorna codice senza reinstallare)
# ==============================================================================
set -euo pipefail

RED='\033[0;31m'; BOLD='\033[1m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "${GREEN}${BOLD}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}${BOLD}  →${NC}  $*"; }
warn() { echo -e "${YELLOW}${BOLD}  ⚠${NC}  $*"; }
err()  { echo -e "${RED}${BOLD}  ✗  $*${NC}"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/backup-all"
SERVICE_NAME="backup-all"

[[ $EUID -ne 0 ]] && err "Esegui come root:  sudo bash update.sh"
[[ -f "${SCRIPT_DIR}/app/main.py" ]] || err "app/main.py non trovato. Esegui dalla radice del repository."

echo -e "\n  ${BOLD}Directory di installazione${NC} ${CYAN}[default: ${INSTALL_DIR}]${NC}"
read -rp "  Percorso (invio per default): " CUSTOM_DIR
[[ -n "$CUSTOM_DIR" ]] && INSTALL_DIR="$CUSTOM_DIR"
[[ -d "$INSTALL_DIR" ]] || err "Directory ${INSTALL_DIR} non trovata. Esegui prima install.sh."

echo ""
info "Aggiornamento da: ${SCRIPT_DIR}"
info "Installato in:    ${INSTALL_DIR}"
echo ""
read -rp "  Procedere? [S/n]: " CONFIRM
[[ "${CONFIRM,,}" == "n" ]] && { echo "  Annullato."; exit 0; }

info "Arresto servizio..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

info "Sincronizzazione file..."
rsync -a --delete \
    --exclude='.venv' \
    --exclude='*.db' \
    --exclude='.secret_key' \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    "${SCRIPT_DIR}/" "${INSTALL_DIR}/"
ok "File aggiornati"

info "Aggiornamento dipendenze Python..."
if "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" --quiet 2>/dev/null; then
    ok "Dipendenze aggiornate"
else
    warn "Alcune dipendenze opzionali non disponibili — core ok"
fi

# Assicura che la chiave Fernet sia nel .env
if ! grep -q "^BACKUP_SECRET_KEY=" "${INSTALL_DIR}/.env" 2>/dev/null; then
    FERNET_KEY=$("${INSTALL_DIR}/.venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "BACKUP_SECRET_KEY=${FERNET_KEY}" >> "${INSTALL_DIR}/.env"
    warn "Chiave di cifratura aggiunta al .env — risalva le password nel pannello"
fi

chown -R backupall:backupall "${INSTALL_DIR}" 2>/dev/null || warn "Permessi non impostati (utente backupall mancante?)"

info "Avvio servizio..."
systemctl start "${SERVICE_NAME}"

for i in $(seq 1 10); do
    sleep 1
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        ok "Servizio attivo dopo ${i}s"
        break
    fi
    if (( i == 10 )); then
        warn "Servizio non partito in 10s — controlla i log:"
        journalctl -u "${SERVICE_NAME}" -n 15 --no-pager | sed 's/^/    /'
        err "Aggiornamento completato ma il servizio non parte."
    fi
done

echo ""
echo -e "${GREEN}${BOLD}  ╔══════════════════════════════════╗"
echo -e "  ║    Aggiornamento completato! ✓   ║"
echo -e "  ╚══════════════════════════════════╝${NC}"
echo -e "  ${CYAN}  Log live:${NC}  journalctl -u ${SERVICE_NAME} -f"
echo ""
