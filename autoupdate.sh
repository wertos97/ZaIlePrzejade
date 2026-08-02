#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOG_FILE="$SCRIPT_DIR/autoupdate.log"
PORT=21112
HEALTH_URL="http://127.0.0.1:$PORT/api/health"

log() {
    local MESSAGE="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$MESSAGE" | tee -a "$LOG_FILE"
}

# --- FUNKCJE WERYFIKACYJNE ---

is_server_healthy() {
    # Próba odpytania curlem, a w razie jego braku – Pythonem
    if command -v curl >/dev/null 2>&1; then
        local HTTP_STATUS
        HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$HEALTH_URL" 2>/dev/null)
        if [ "$HTTP_STATUS" -eq 200 ]; then
            return 0
        fi
    else
        if python3 -c "import urllib.request; res = urllib.request.urlopen('$HEALTH_URL', timeout=3); exit(0 if res.getcode() == 200 else 1)" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Zwraca 0 (OK) lub wypisuje konkretny powód błędu i zwraca 1
get_data_integrity_status() {
    if [ ! -d "processed" ]; then
        echo "Brak katalogu processed/"
        return 1
    fi
    
    local COUNT
    COUNT=$(ls -1 processed/*.json 2>/dev/null | wc -l)
    if [ "$COUNT" -eq 0 ]; then
        echo "Katalog processed/ jest pusty"
        return 1
    fi

    for file in processed/*.json; do
        if [ -f "$file" ]; then
            FILE_SIZE=$(wc -c < "$file")
            if [ "$FILE_SIZE" -lt 100 ]; then
                echo "Plik $file jest uszkodzony lub za mały ($FILE_SIZE B)"
                return 1
            fi
        fi
    done

    if [ ! -f "server.py" ] || [ ! -s "server.py" ]; then
        echo "Brakujący lub pusty plik główny: server.py"
        return 1
    fi

    return 0
}

sync_with_git() {
    if [ -d ".git" ]; then
        log "📥 Pobieranie najnowszej wersji kodu z GitHub..."
        git fetch origin >> "$LOG_FILE" 2>&1
        git checkout HEAD -- processed/ >> "$LOG_FILE" 2>&1
        git reset --hard origin/$(git rev-parse --abbrev-ref HEAD) >> "$LOG_FILE" 2>&1
        git clean -fd -e "*.sh" -e "*.log" -e "venv/" >> "$LOG_FILE" 2>&1
        git pull >> "$LOG_FILE" 2>&1
    fi
}

wait_for_health() {
    # Dajemy serwerowi do 30 sekund (15 prób co 2 sekundy) na pełny rozruch
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

# --- GŁÓWNA LOGIKA ---

UPDATED=false
REASON=""

if [ -d ".git" ]; then
    git fetch origin > /dev/null 2>&1
    LOCAL_HASH=$(git rev-parse HEAD)
    REMOTE_HASH=$(git rev-parse @{u} 2>/dev/null || echo "$LOCAL_HASH")

    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
        UPDATED=true
        REASON="Wykryto nowe commity na GitHubie ($LOCAL_HASH -> $REMOTE_HASH)."
    fi
fi

INTEGRITY_ERR=$(get_data_integrity_status)
INTEGRITY_RET=$?

if [ "$UPDATED" = false ] && [ $INTEGRITY_RET -ne 0 ]; then
    REASON="Błąd spójności danych: $INTEGRITY_ERR"
elif [ "$UPDATED" = false ] && ! is_server_healthy; then
    REASON="Serwer nie odpowiada na $HEALTH_URL."
fi

# Jeśli serwer działa (HTTP 200), dane są OK i brak zmian w repo -> Wyjdź cicho
if [ "$UPDATED" = false ] && [ $INTEGRITY_RET -eq 0 ] && is_server_healthy; then
    log "ℹ️ Status: OK. Serwer odpowiada na /api/health (200 OK), brak nowych aktualizacji."
    exit 0
fi

# --- PROCEDURA NAPRAWY / UPDATE ---

log "=========================================="
log "🚀 Rozpoczynanie auto-naprawy / aktualizacji."
log "📌 Powód: $REASON"
log "=========================================="

# Zaciągnięcie najnowszego kodu przed próba restartu/naprawy
sync_with_git

# Obsługa środowiska venv
if [ ! -f "venv/bin/activate" ]; then
    log "⚠️ Brak środowiska venv. Tworzenie nowe..."
    python3 -m venv venv >> "$LOG_FILE" 2>&1
fi

# shellcheck disable=SC1091
source venv/bin/activate

if [ -f "requirements.txt" ]; then
    log "📦 Wykryto requirements.txt, instalowanie zależności..."
    pip install -r requirements.txt >> "$LOG_FILE" 2>&1
fi

# Pierwszy restart
if [ -f "./restart.sh" ]; then
    bash ./restart.sh
else
    fuser -k -9 $PORT/tcp >/dev/null 2>&1
    pkill -9 -f "server.py" >/dev/null 2>&1
    PORT=$PORT nohup python3 -u server.py >> server.log 2>&1 &
fi

log "⏳ Czekanie na załadowanie indeksów w RAM i odpowiedź z $HEALTH_URL (max 30s)..."
if wait_for_health; then
    log "=========================================="
    log "🎉 SUKCES: Serwer pomyślnie przeszedł test /api/health!"
    if [ -d ".git" ]; then
        log "📌 Commit: $(git log -1 --format="%h - %s (%cr) <%an>")"
    fi
    log "=========================================="
    exit 0
fi

# Pętla ratunkowa w razie dalszych problemów
MAX_RETRIES=2
RETRY_COUNT=0

while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    RETRY_COUNT=$((RETRY_COUNT + 1))
    log "⚠️ WARN: Brak odpowiedzi HTTP 200. Ponowne pobieranie kodu z Git i próba $RETRY_COUNT z $MAX_RETRIES..."
    
    if [ -f "server.log" ]; then
        log "🔍 Ostatnie 5 linii z server.log:"
        tail -n 5 server.log | while read -r line; do log "   $line"; done
    fi

    # Pobranie kodu ponownie
    sync_with_git

    if [ -f "./restart.sh" ]; then
        bash ./restart.sh
    else
        fuser -k -9 $PORT/tcp >/dev/null 2>&1
        pkill -9 -f "server.py" >/dev/null 2>&1
        PORT=$PORT nohup python3 -u server.py >> server.log 2>&1 &
    fi
    
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
