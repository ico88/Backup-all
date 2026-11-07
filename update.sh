#!/usr/bin/env bash
# Aggiornamento codice Backup-All senza reinstallazione completa
set -euo pipefail

INSTALL_DIR="/opt/backup-all"
SERVICE_NAME="backup-all"
REPO_URL="https://github.com/ico88/Backup-all.git"
REPO_BRANCH="claude/loving-keller-d6978c"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}${BOLD}  ✓${NC}  $*"; }
info() { echo -e "${CYAN}${BOLD}  →${NC}  $*"; }
err()  { echo -e "${RED}${BOLD}  ✗${NC}  $*"; exit 1; }

[[ $EUID -ne 0 ]] && err "Esegui come root: sudo bash update.sh"
[[ ! -d "$INSTALL_DIR" ]] && err "Installazione non trovata in $INSTALL_DIR. Esegui install.sh prima."

info "Arresto servizio..."
systemctl stop "$SERVICE_NAME" || true

info "Aggiornamento codice da GitHub (branch: $REPO_BRANCH)..."
TMP_CLONE=$(mktemp -d)
git clone --branch "$REPO_BRANCH" --depth 1 "$REPO_URL" "$TMP_CLONE" 2>&1 | sed 's/^/    /'

rsync -a --delete \
  --exclude='.venv/' \
  --exclude='*.db' \
  --exclude='.env' \
  --exclude='.secret_key' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "$TMP_CLONE/" "$INSTALL_DIR/"

rm -rf "$TMP_CLONE"
ok "Codice aggiornato"

info "Aggiornamento dipendenze Python..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
ok "Dipendenze aggiornate"

# Aggiungi chiave Fernet al .env se mancante
if [[ -f "$INSTALL_DIR/.env" ]] && ! grep -q "^BACKUP_SECRET_KEY=" "$INSTALL_DIR/.env"; then
  if [[ -f "$INSTALL_DIR/.secret_key" ]]; then
    KEY=$(cat "$INSTALL_DIR/.secret_key")
  else
    KEY=$("$INSTALL_DIR/.venv/bin/python" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "$KEY" > "$INSTALL_DIR/.secret_key"
  fi
  echo "BACKUP_SECRET_KEY=$KEY" >> "$INSTALL_DIR/.env"
  ok "Chiave Fernet aggiunta a .env"
fi

chown -R backupall:backupall "$INSTALL_DIR" 2>/dev/null || true

info "Avvio servizio..."
systemctl start "$SERVICE_NAME"
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
  ok "Servizio avviato correttamente"
else
  echo "  Controlla i log: journalctl -u $SERVICE_NAME -n 50"
fi
