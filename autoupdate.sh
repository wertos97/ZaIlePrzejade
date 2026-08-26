#!/bin/bash
# ============================================================
# Auto-update i self-recovery dla serwera MPK
#
# Ten skrypt:
#   - sprawdza czy na GitHubie są nowe commity
#   - weryfikuje spójność danych (processed/)
#   - sprawdza czy serwer odpowiada na /api/health
#   - w razie potrzeby aktualizuje kod i restartuje serwer
#
# Konfiguracja (port, URL zdrowia, gałąź) jest ładowana z
# `server.env` (ignorowanego przez git) - ten plik NIE zawiera
# żadnych sekretów ani szczegółów konkretnego serwera.
#
# Użycie: uruchamiany przez cron co 2 minuty:
#   */2 * * * * /ścieżka/do/autoupdate.sh
# ============================================================

set -euo pipefail

# --- Wczytaj wspólne funkcje ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

# --- Blokada concurrency ---
acquire_lock

# --- Pliki, których NIGDY nie nadpisujemy podczas aktualizacji ---
# (skrypty, konfiguracja, logi, środowisko, surowe dane)
PROTECTED_FILES=(
    "autoupdate.sh"
    "restart.sh"
    "common.sh"
    "server.env"
    "server.env.example"
    "start.sh"
    "stop.sh"
    "generate-assets.sh"
    "preview-logo.sh"
    ".gitignore"
    "README.md"
)

# --- Synchronizacja z git (bez nadpisywania chronionych plików) ---
sync_with_git() {
    if [ ! -d "$SCRIPT_DIR/.git" ]; then
        log "⚠️ Brak katalogu .git - pomijam synchronizację z GitHub."
        return 0
    fi

    log "📥 Synchronizacja kodu z GitHub (gałąź: $GIT_BRANCH)..."

    # Fetch został już wykonany wcześniej (porównanie hashy) — tu tylko reset
    # Backup chronionych plików przed resetem
    local BACKUP_DIR
    BACKUP_DIR=$(mktemp -d)
    local backed_up=false
    for f in "${PROTECTED_FILES[@]}"; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            mkdir -p "$BACKUP_DIR/$(dirname "$f")"
            cp "$SCRIPT_DIR/$f" "$BACKUP_DIR/$f"
            backed_up=true
        fi
    done

    # Pokaż co się zmienia przed resetem
    log "📊 Zmiany w stosunku do origin/$GIT_BRANCH:"
    git -C "$SCRIPT_DIR" diff --stat "HEAD" "origin/$GIT_BRANCH" >> "$LOG_FILE" 2>&1 || true

    # Zresetuj do stanu zdalnego
    git -C "$SCRIPT_DIR" reset --hard "origin/$GIT_BRANCH" >> "$LOG_FILE" 2>&1 || {
        log "❌ Błąd podczas git reset."
        rm -rf "$BACKUP_DIR"
        return 1
    }

    # Wyczyść nieśledzone pliki, ale zachowaj katalogi lokalne i chronione pliki
    git -C "$SCRIPT_DIR" clean -fd \
        --exclude='*.log' \
        --exclude='venv/' \
        --exclude='data/' \
        --exclude='processed/' \
        --exclude='__pycache__/' \
        --exclude='public/vendor/' \
        --exclude='server/' \
        --exclude='autoupdate.sh' \
        --exclude='restart.sh' \
        --exclude='common.sh' \
        --exclude='server.env' \
        --exclude='server.env.example' \
        --exclude='start.sh' \
        --exclude='stop.sh' \
        --exclude='generate-assets.sh' \
        --exclude='preview-logo.sh' \
        --exclude='.gitignore' \
        --exclude='README.md' \
        >> "$LOG_FILE" 2>&1 || true

    # --- Przywróć chronione pliki z backupu ---
    if [ "$backed_up" = true ]; then
        for f in "${PROTECTED_FILES[@]}"; do
            if [ -f "$BACKUP_DIR/$f" ]; then
                mkdir -p "$SCRIPT_DIR/$(dirname "$f")"
                cp "$BACKUP_DIR/$f" "$SCRIPT_DIR/$f"
            fi
        done
    fi
    rm -rf "$BACKUP_DIR"

    log "✅ Kod zsynchronizowany. Commit: $(git -C "$SCRIPT_DIR" log -1 --format="%h - %s")"
    return 0
}

# --- Główna logika ---
load_config

UPDATED=false
REASON=""

# 1. Sprawdź czy są nowe commity na GitHubie
if [ -d "$SCRIPT_DIR/.git" ]; then
    git -C "$SCRIPT_DIR" fetch origin "$GIT_BRANCH" > /dev/null 2>&1 || true
    LOCAL_HASH=$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || echo "unavailable")
    REMOTE_HASH=$(git -C "$SCRIPT_DIR" rev-parse "origin/$GIT_BRANCH" 2>/dev/null || echo "$LOCAL_HASH")

    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
        UPDATED=true
        REASON="Wykryto nowe commity na GitHubie ($LOCAL_HASH -> $REMOTE_HASH)."
    fi
fi

# 2. Weryfikacja spójności danych
# (jawnie przechwytujemy kod wyjścia — przy `set -e` przypisanie z
# nieudaną substytucją komendy ubiłoby skrypt zamiast uruchomić naprawę)
INTEGRITY_RET=0
INTEGRITY_ERR=""
if ! INTEGRITY_ERR=$(get_data_integrity_status); then
    INTEGRITY_RET=1
fi

# 3. Sprawdzenie zdrowia serwera
SERVER_HEALTHY=false
if is_server_healthy; then
    SERVER_HEALTHY=true
fi

# 4. Określ powód działania
if [ "$UPDATED" = false ] && [ $INTEGRITY_RET -ne 0 ]; then
    REASON="Błąd spójności danych: $INTEGRITY_ERR"
elif [ "$UPDATED" = false ] && [ "$SERVER_HEALTHY" = false ]; then
    REASON="Serwer nie odpowiada na $HEALTH_URL."
fi

# 5. Jeśli wszystko OK i brak zmian -> wyjdź CICHO.
# Cron odpala ten skrypt co 2 minuty, więc sukces nie może zaśmiecać loga —
# zapisujemy wyłącznie problemy i podjęte akcje.
if [ "$UPDATED" = false ] && [ $INTEGRITY_RET -eq 0 ] && [ "$SERVER_HEALTHY" = true ]; then
    exit 0
fi

# --- Procedura naprawy / aktualizacji ---
log "=========================================="
log "🚀 Rozpoczynanie auto-naprawy / aktualizacji."
log "📌 Powód: $REASON"
log "=========================================="

# Zaciągnij najnowszy kod (bez nadpisywania chronionych plików)
sync_with_git || {
    log "❌ Nie udało się zsynchronizować kodu. Przechodzę do restartu z obecnym kodem."
}

# Obsługa środowiska venv (opcjonalne - aplikacja używa tylko biblioteki standardowej)
PYTHON_BIN="python3"
if [ ! -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    log "⚠️ Brak środowiska venv. Próbuję utworzyć (opcjonalne)..."
    if python3 -m venv "$SCRIPT_DIR/venv" >> "$LOG_FILE" 2>&1; then
        log "✅ Środowisko venv utworzone."
    else
        log "⚠️ Nie udało się utworzyć venv (brak python3-venv?). Używam systemowego Pythona."
        rm -rf "$SCRIPT_DIR/venv"
    fi
fi

if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/venv/bin/activate"
    PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"
fi

# Restart serwera
restart_server

log "⏳ Czekanie na załadowanie danych i odpowiedź z $HEALTH_URL (max 30s)..."
if wait_for_health; then
    log "=========================================="
    log "🎉 SUKCES: Serwer pomyślnie przeszedł test /api/health!"
    if [ -d "$SCRIPT_DIR/.git" ]; then
        log "📌 Commit: $(git -C "$SCRIPT_DIR" log -1 --format="%h - %s (%cr) <%an>")"
    fi
    log "=========================================="
    exit 0
fi

# --- Pętla ratunkowa w razie dalszych problemów ---
MAX_RETRIES=2
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    log "⚠️ WARN: Brak odpowiedzi HTTP 200. Ponowne pobieranie kodu z Git i próba $RETRY_COUNT z $MAX_RETRIES..."

    if [ -f "$SCRIPT_DIR/server.log" ]; then
        log "🔍 Ostatnie 5 linii z server.log:"
        tail -n 5 "$SCRIPT_DIR/server.log" | while read -r line; do log "   $line"; done
    fi

    # Pobranie kodu ponownie (bez nadpisywania chronionych plików)
    sync_with_git || true

    restart_server

    log "⏳ Czekanie na odpowiedź z $HEALTH_URL (max 30s)..."
    if wait_for_health; then
        log "🎉 SUKCES: Serwer odpowiada na /api/health przy próbie $RETRY_COUNT!"
        exit 0
    fi
done

log "=========================================="
log "❌ AWARIA KRYTYCZNA: Serwer nie przeszedł testu /api/health po próbach naprawy."
log "=========================================="
exit 1