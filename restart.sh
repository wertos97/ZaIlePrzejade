#!/bin/bash
# ============================================================
# Restart serwera MPK
#
# Zatrzymuje i uruchamia serwer ponownie.
# Konfiguracja (port, tryb tła) jest ładowana z `server.env`
# (ignorowanego przez git) - ten plik NIE zawiera sekretów.
#
# Użycie: bash restart.sh
# ============================================================

set -euo pipefail

# --- Wczytaj wspólne funkcje ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

load_config

log "🔄 Restartowanie serwera na porcie $PORT..."

# Restart serwera (stop + start)
if restart_server; then
    log "✅ Serwer został zrestartowany."
    exit 0
else
    log "❌ Restart serwera nie powiódł się."
    exit 1
fi