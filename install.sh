#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  install.sh — Installation de Pool Manager sous Debian/Ubuntu           ║
# ║  Usage : sudo bash install.sh                                           ║
# ║  Réinstallation/mise à jour : sudo bash install.sh --update             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
APP_DIR="/opt/pool"
APP_USER="poolmgr"
APP_GROUP="poolmgr"
SERVICE="pool-manager"
PORT=5000
PYTHON="python3"

# ── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()      { echo -e "${GREEN}  ✓${NC}  $*"; }
warn()    { echo -e "${YELLOW}  ⚠${NC}  $*"; }
err()     { echo -e "${RED}  ✗${NC}  $*" >&2; exit 1; }
section() { echo -e "\n${BLUE}${BOLD}══ $* ══${NC}"; }
info()    { echo -e "${CYAN}  ➜${NC}  $*"; }

# ── Bannière ─────────────────────────────────────────────────────────────────
echo -e "${BLUE}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   🏊  Pool Manager — Installation       ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Vérifications préalables ─────────────────────────────────────────────────
section "Vérifications"

[[ $EUID -eq 0 ]] || err "Ce script doit être exécuté en tant que root (sudo bash install.sh)"

# Détecte Debian / Ubuntu / dérivés
if [[ ! -f /etc/debian_version ]]; then
  warn "Ce script est prévu pour Debian/Ubuntu. Continuer quand même ? [o/N]"
  read -r ans
  [[ "$ans" =~ ^[oOyY]$ ]] || exit 0
fi
ok "Système compatible détecté"

# Répertoire source = là où se trouve ce script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Répertoire source : $SCRIPT_DIR"

UPDATE=false
[[ "${1:-}" == "--update" ]] && UPDATE=true

# ── Dépendances système ───────────────────────────────────────────────────────
section "Installation des dépendances système"

apt-get update -qq
apt-get install -y -qq \
  python3 \
  python3-venv \
  python3-pip \
  curl \
  > /dev/null

ok "Python3, venv, pip installés"
$PYTHON --version | sed "s/^/       /"

# ── Utilisateur système dédié ─────────────────────────────────────────────────
section "Création de l'utilisateur système"

if id "$APP_USER" &>/dev/null; then
  ok "Utilisateur '$APP_USER' déjà existant"
else
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --comment "Pool Manager service user" "$APP_USER"
  ok "Utilisateur système '$APP_USER' créé"
fi

# ── Répertoire de l'application ───────────────────────────────────────────────
section "Création du répertoire $APP_DIR"

mkdir -p "$APP_DIR"
ok "Répertoire $APP_DIR prêt"

# ── Copie des fichiers source ─────────────────────────────────────────────────
section "Copie des fichiers de l'application"

# Arrêter le service avant mise à jour pour éviter les conflits de fichiers
if $UPDATE && systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  systemctl stop "$SERVICE"
  ok "Service arrêté pour la mise à jour"
fi

FILES=(app.py recommendations.py requirements.txt)
for f in "${FILES[@]}"; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$APP_DIR/$f"
    ok "Copié : $f"
  else
    err "Fichier manquant dans $SCRIPT_DIR : $f"
  fi
done

# Templates et static
for dir in templates static; do
  if [[ -d "$SCRIPT_DIR/$dir" ]]; then
    cp -r "$SCRIPT_DIR/$dir" "$APP_DIR/"
    ok "Copié : $dir/"
  else
    err "Répertoire manquant : $SCRIPT_DIR/$dir"
  fi
done

# ── Environnement virtuel Python ──────────────────────────────────────────────
section "Environnement virtuel Python"

if [[ ! -d "$APP_DIR/venv" ]]; then
  $PYTHON -m venv "$APP_DIR/venv"
  ok "Virtualenv créé"
else
  ok "Virtualenv existant conservé"
fi

info "Installation des paquets Python..."
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
ok "Dépendances Python installées"

# ── Clé secrète Flask ────────────────────────────────────────────────────────
section "Configuration"

SECRET_FILE="$APP_DIR/.env"
if [[ ! -f "$SECRET_FILE" ]]; then
  SECRET_KEY=$($PYTHON -c "import secrets; print(secrets.token_hex(32))")
  echo "SECRET_KEY=$SECRET_KEY" > "$SECRET_FILE"
  ok "Clé secrète Flask générée"
else
  ok "Clé secrète existante conservée"
fi

# ── Permissions ───────────────────────────────────────────────────────────────
section "Permissions"

chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod 750 "$APP_DIR"
chmod 640 "$SECRET_FILE"
# La base SQLite doit être accessible en écriture
touch "$APP_DIR/pool.db" 2>/dev/null || true
chown "$APP_USER:$APP_GROUP" "$APP_DIR/pool.db" 2>/dev/null || true
ok "Permissions appliquées (propriétaire : $APP_USER)"

# ── Service systemd ───────────────────────────────────────────────────────────
section "Service systemd"

cat > "/etc/systemd/system/${SERVICE}.service" << EOF
[Unit]
Description=Pool Manager - Gestionnaire de piscine
Documentation=https://github.com/
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${SECRET_FILE}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/app.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

# Sécurité
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${APP_DIR}
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

ok "Fichier service créé : /etc/systemd/system/${SERVICE}.service"

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# Attendre le démarrage
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  ok "Service '$SERVICE' démarré et activé au boot"
else
  warn "Le service ne semble pas actif. Vérifiez les logs :"
  echo ""
  journalctl -u "$SERVICE" --no-pager -n 20
  exit 1
fi

# ── Résumé ────────────────────────────────────────────────────────────────────
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

echo ""
echo -e "${GREEN}${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}${BOLD}║   ✅  Installation terminée avec succès !            ║${NC}"
echo -e "${GREEN}${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Application accessible sur :${NC}"
echo -e "    🌐  http://localhost:${PORT}"
echo -e "    🌐  http://${LOCAL_IP}:${PORT}"
echo ""
echo -e "  ${BOLD}Commandes utiles :${NC}"
echo -e "    ${CYAN}systemctl status ${SERVICE}${NC}      → état du service"
echo -e "    ${CYAN}systemctl restart ${SERVICE}${NC}     → redémarrer"
echo -e "    ${CYAN}journalctl -u ${SERVICE} -f${NC}      → logs en direct"
echo -e "    ${CYAN}systemctl stop ${SERVICE}${NC}        → arrêter"
echo ""
echo -e "  ${BOLD}Données persistantes :${NC}  ${APP_DIR}/pool.db"
echo -e "  ${BOLD}Logs système :${NC}          journalctl -u ${SERVICE}"
echo ""
echo -e "  ${YELLOW}Pour mettre à jour l'application :${NC}"
echo -e "    ${CYAN}sudo bash install.sh --update${NC}"
echo ""
