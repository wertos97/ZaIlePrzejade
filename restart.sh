#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

LOG_FILE="$SCRIPT_DIR/autoupdate.log"
PORT=21112

log() {
    local MESSAGE="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$MESSAGE" | tee -a "$LOG_FILE"
}

log "🔄 Wywoływanie restart.sh: Restartowanie serwera na porcie $PORT..."

# 1. Bezwzględne zwolnienie portu TCP i zatrzymanie starych procesów Pythona
if pgrep -f "server.py" > /dev/null || fuser $PORT/tcp >/dev/null 2>&1; then
    log "⏹️ Wykryto uruchomiony proces/port. Zatrzymywanie..."
    fuser -k -9 $PORT/tcp >/dev/null 2>&1
    pkill -9 -f "server.py" >/dev/null 2>&1
    sleep 2
fi

# 2. Aktywacja środowiska wirtualnego (jeśli istnieje)
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

# 3. Uruchomienie serwera w tle z przekierowaniem logów
PORT=$PORT nohup python3 -u server.py >> server.log 2>&1 &

# 4. Krótka weryfikacja procesowa
sleep 2
if pgrep -f "server.py" > /dev/null; then
    log "🚀 Proces server.py został zrestartowany w tle."
else
    log "❌ Próba uruchomienia w restart.sh nie powiodła się."
    exit 1
fi

exit 0
