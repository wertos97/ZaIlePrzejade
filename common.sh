#!/bin/bash
# ============================================================
# Współdzielone funkcje dla skryptów serwera MPK
# (autoupdate.sh, restart.sh)
#
# Ten plik jest źródłem wspólnej logiki. Nie zawiera żadnych
# sekretów ani szczegółów konkretnego serwera - cała konfiguracja
# jest ładowana z pliku `server.env` (ignorowanego przez git).
# ============================================================

# --- PATH dla cron (cron działa w minimalnym środowisku bez pełnego PATH) ---
# Ustaw jawnie, aby skrypty znalazły git, curl, python3, fuser, pgrep, nohup
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

# --- Ścieżki ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/server.env"
LOG_FILE="$SCRIPT_DIR/autoupdate.log"

# --- Domyślne wartości (bezpieczne, bez sekretów) ---
DEFAULT_PORT=8080
DEFAULT_RUN_IN_BACKGROUND=true

# --- Ładowanie konfiguracji z server.env (jeśli istnieje) ---
# Wczytuje zmienne PORT, HEALTH_URL, GIT_BRANCH, RUN_IN_BACKGROUND
load_config() {
    if [ -f "$ENV_FILE" ]; then
        # shellcheck disable=SC1090
        source "$ENV_FILE"
    fi

    # Ustaw domyślne wartości, jeśli nie zostały zdefiniowane
    PORT="${PORT:-$DEFAULT_PORT}"
    RUN_IN_BACKGROUND="${RUN_IN_BACKGROUND:-$DEFAULT_RUN_IN_BACKGROUND}"
    HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:$PORT/api/health}"
    GIT_BRANCH="${GIT_BRANCH:-$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'main')}"
}

# --- Logowanie ---
log() {
    local MESSAGE="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$MESSAGE" | tee -a "$LOG_FILE"
}

# --- Sprawdzanie zdrowia serwera ---
# Zwraca 0 jeśli serwer odpowiada HTTP 200 na /api/health
is_server_healthy() {
    if command -v curl >/dev/null 2>&1; then
        local HTTP_STATUS
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$HEALTH_URL" 2>/dev/null)
        [ "$HTTP_STATUS" -eq 200 ]
    else
        python3 -c "import urllib.request; res = urllib.request.urlopen('$HEALTH_URL', timeout=3); exit(0 if res.getcode() == 200 else 1)" 2>/dev/null
    fi
}

# --- Czekanie na zdrowie serwera (max 30s) ---
wait_for_health() {
    local RETRIES=15
    while [ $RETRIES -gt 0 ]; do
        if is_server_healthy; then
            return 0
        fi
        sleep 2
        RETRIES=$((RETRIES - 1))
    done
    return 1
}

# --- Zatrzymanie serwera ---
stop_server() {
    # Zwolnij port TCP (|| true - fuser zwraca 1 gdy port jest wolny)
    fuser -k -9 "$PORT/tcp" >/dev/null 2>&1 || true
    # Zatrzymaj procesy Pythona uruchamiające server.py,
    # ale NIE zabijaj samego skryptu (autoupdate.sh/restart.sh),
    # których ścieżka też zawiera "server.py"
    local SELF_PID=$$
    for pid in $(pgrep -f "python3.*server\.py" 2>/dev/null || true); do
        if [ "$pid" != "$SELF_PID" ]; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    sleep 2
}

# --- Uruchomienie serwera ---
# Używa PYTHON_BIN jeśli ustawione (np. przez autoupdate.sh), w przeciwnym razie python3
start_server() {
    local PY="${PYTHON_BIN:-python3}"
    if [ "$RUN_IN_BACKGROUND" = "true" ]; then
        PORT="$PORT" nohup "$PY" -u "$SCRIPT_DIR/server.py" >> "$SCRIPT_DIR/server.log" 2>&1 &
    else
        PORT="$PORT" "$PY" -u "$SCRIPT_DIR/server.py"
    fi
}

# --- Restart serwera (stop + start) ---
restart_server() {
    stop_server
    start_server
    sleep 2
    if pgrep -f "server.py" > /dev/null; then
        log "🚀 Proces server.py został uruchomiony."
        return 0
    else
        log "❌ Próba uruchomienia serwera nie powiodła się."
        return 1
    fi
}

# --- Weryfikacja spójności danych ---
# Zwraca 0 (OK) lub wypisuje powód błędu i zwraca 1
get_data_integrity_status() {
    if [ ! -d "$SCRIPT_DIR/processed" ]; then
        echo "Brak katalogu processed/"
        return 1
    fi

    local COUNT
    COUNT=$(ls -1 "$SCRIPT_DIR"/processed/*.json 2>/dev/null | wc -l)
    if [ "$COUNT" -eq 0 ]; then
        echo "Katalog processed/ jest pusty"
        return 1
    fi

    for file in "$SCRIPT_DIR"/processed/*.json; do
        if [ -f "$file" ]; then
            local FILE_SIZE
            FILE_SIZE=$(wc -c < "$file")
            if [ "$FILE_SIZE" -lt 100 ]; then
                echo "Plik $file jest uszkodzony lub za mały ($FILE_SIZE B)"
                return 1
            fi
        fi
    done

    if [ ! -f "$SCRIPT_DIR/server.py" ] || [ ! -s "$SCRIPT_DIR/server.py" ]; then
        echo "Brakujący lub pusty plik główny: server.py"
        return 1
    fi

    return 0
}