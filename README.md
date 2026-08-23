<p align="center">
  <img src="public/logo.svg" alt="Za Ile Przejadę?" width="120" />
</p>

<h1 align="center">🚌🚊 Za Ile Przejadę?</h1>

<p align="center">
  <strong>Kalkulator cen biletów komunikacji miejskiej w Krakowie 2027</strong><br/>
  Oblicz koszt podróży komunikacją miejską w Krakowie w oparciu o nowy system biletów opartych na odległości.
</p>

<p align="center">
  <a href="https://zaileprzeja.de"><strong>🌐 zaileprzeja.de</strong></a>
</p>

---

## ✨ Funkcjonalności

- 🔍 **Wyszukiwanie przystanków** po nazwie (z autouzupełnianiem)
- 🗺️ **Wybór przystanków na mapie** (Leaflet)
- 💰 **Obliczanie dystansu i kosztu przejazdu** wg nowego systemu biletów
- 🛤️ **Dwa tryby nawigacji**:
  - **Tania trasa** — najtańszy przejazd (każdy segment to osobny bilet, więc najtańsze nie zawsze = najkrótsze)
  - **Wygodna trasa** — najmniej przesiadek
- 📍 **Wyświetlanie trasy na mapie** z ceną i dystansem nad każdym przejazdem
- 📱 **Responsywny interfejs** (mobile + desktop)
- 🔗 **Udostępnianie tras** przez URL (np. `?from=group_342&to=group_587&mode=short`)
- 🖼️ **Dynamiczne OG image** dla social media (Facebook, Twitter, Telegram, WhatsApp...)

## 🖼️ Przykładowe OG image

| Statyczny | Dynamiczny (API) |
|-----------|------------------|
| ![OG image](previews/og-image.svg) | ![OG image API](previews/og-image-api.svg) |

> Podglądy generowane przez `preview-logo.sh`. Dynamiczny OG image jest generowany na żywo przez `/api/og-image` z nazwami przystanków i ceną.

## 💰 Model cen (2027)

Nowy system biletów komunikacji miejskiej w Krakowie oparty jest na **odległości**. Każdy przejazd (segment między przesiadkami) to **osobny bilet**, wyceniany od zera.

| Parametr | Wartość |
|----------|---------|
| Bilet bazowy (do 3,5 km) | 4,00 zł / 2,00 zł (ulgowy) |
| Każde kolejne 0,5 km | +0,50 zł / +0,25 zł |
| Maksymalna cena pojedynczego biletu | 9,00 zł / 4,50 zł |
| **Limit dzienny** | **20,00 zł / 10,00 zł** |

> Po osiągnięciu limitu dziennego kolejne przejazdy tego dnia są **bezpłatne**.

Konfiguracja cen znajduje się w [`pricing.json`](pricing.json) i jest ładowana przy starcie serwera.

## 🏗️ Architektura

```
┌─────────────────────────────────────────────────────┐
│                Klient (przeglądarka)                │
│            HTML/CSS/JS + Leaflet.js (mapy)          │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP/JSON
┌──────────────────────▼──────────────────────────────┐
│             Serwer (Python, stdlib only)            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  A* path    │  │  Rate limit │  │  Cache      │  │
│  │  finding    │  │  (per IP)   │  │  (bounded)  │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │
│  │  Pricing    │  │  OG image   │  │  Gzip       │  │
│  │  engine     │  │  generator  │  │  compression│  │
│  └─────────────┘  └─────────────┘  └─────────────┘  │
└──────────────────────┬──────────────────────────────┘
                       │
        ┌──────────────▼──────────────┐
        │  processed/ (GTFS → JSON)   │
        │  stops, routes, adjacency,  │
        │  shapes, transfers          │
        └─────────────────────────────┘
```

### Technologie

- **Backend**: Python 3 (tylko biblioteka standardowa — zero zależności)
- **Frontend**: HTML/CSS/JavaScript (vanilla)
- **Mapy**: Leaflet.js (hostowany lokalnie)
- **Dane**: GTFS publikowane przez ZTP Kraków → przetworzone do JSON (`process_gtfs.py`)

### Optymalizacje

- 🧵 **Threading** z limitem równoczesnych żądań (ochrona przed przeciążeniem)
- 💾 **Cache tras** (ograniczony rozmiarem) — powtarzające się zapytania są natychmiastowe
- 🗜️ **Gzip compression** dla odpowiedzi JSON > 1KB
- ⚡ **Pre-komputowane odpowiedzi** dla `/api/stops` i `/api/routes`
- 🔥 **Warmup cache** przy starcie (popularne pary przystanków)
- 🚀 **1 żądanie na trasę** (tryb wybierany leniwie, nie 2 naraz)

## 🚀 Uruchomienie lokalne

Wymagania: **Python 3.8+** (bez dodatkowych pakietów).

```bash
# 1. Sklonuj repozytorium
git clone https://github.com/wertos97/ZaIlePrzejade.git
cd ZaIlePrzejade

# 2. (Opcjonalnie) Przetwórz dane GTFS
#    Umieść pliki GTFS w data/ i uruchom:
#    python process_gtfs.py

# 3. Uruchom serwer
python server.py
```

Serwer uruchomi się na `http://localhost:8080` (port można zmienić przez zmienną `PORT`).

```bash
PORT=3000 python server.py
```

## 📡 API

| Endpoint | Opis |
|----------|------|
| `GET /api/stops` | Wszystkie przystanki (zgrupowane) |
| `GET /api/stops/search?q=<query>` | Wyszukiwanie przystanków |
| `GET /api/stop-platforms?id=<group_id>` | Platformy przystanku |
| `GET /api/find-route?from=<id>&to=<id>&mode=<short\|convenient>` | Znajdź trasę |
| `GET /api/cost?distance=<km>` | Oblicz koszt dla dystansu |
| `GET /api/shapes?route_id=<id>` | Kształt linii |
| `GET /api/routes` | Wszystkie linie |
| `GET /api/stop?id=<id>` | Informacje o przystanku |
| `GET /api/og-image?from=<id>&to=<id>&mode=<short\|convenient>` | Dynamiczny OG image (SVG) |
| `GET /api/health` | Health check |
| `GET /api/version` | Wersja aplikacji |

### Przykład

```bash
curl "http://localhost:8080/api/find-route?from=group_1&to=group_50&mode=short"
```

```json
{
  "total_distance": 10.62,
  "cost_regular": 19.5,
  "cost_reduced": 9.75,
  "max_daily_cost_regular": 20.0,
  "max_daily_cost_reduced": 10.0,
  "path": [...],
  "segments": [...],
  "transfers": [...]
}
```

## 🔄 Auto-aktualizacje i self-recovery

Repozytorium zawiera skrypty do utrzymania serwera w ruchu **bez ręcznej interwencji**:

| Skrypt | Rola |
|--------|------|
| **`autoupdate.sh`** | Sprawdza czy na GitHubie są nowe commity, weryfikuje spójność danych i zdrowie serwera. W razie potrzeby aktualizuje kod i restartuje serwer. |
| **`restart.sh`** | Zatrzymuje i uruchamia serwer ponownie. |
| **`common.sh`** | Współdzielone funkcje (PATH dla cron, logowanie, health check, restart). |

### Jak to działa

1. **Cron** cyklicznie uruchamia `autoupdate.sh`
2. Skrypt **pobiera zmiany** z GitHub (`git fetch` + `git reset --hard`)
3. **Weryfikuje spójność** danych (`processed/`) i zdrowie serwera (`/api/health`)
4. Jeśli coś jest nie tak — **aktualizuje kod i restartuje serwer**
5. W razie awarii — **pętla ratunkowa** (2 próby pobrania kodu i restartu)

### Konfiguracja

Skrypty czytają konfigurację z pliku `server.env` (ignorowanego przez git):

```bash
cp server.env.example server.env
```

Dostępne zmienne (wszystkie opcjonalne):

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `PORT` | `8080` | Port serwera |
| `HEALTH_URL` | `http://127.0.0.1:$PORT/api/health` | URL do sprawdzania zdrowia |
| `GIT_BRANCH` | bieżąca | Gałąź git do śledzenia |
| `RUN_IN_BACKGROUND` | `true` | Czy uruchamiać serwer w tle |
| `TRUST_PROXY_HEADERS` | `true` | Ufać nagłówkom `X-Real-IP`/`X-Forwarded-For`; ustaw `false` przy braku reverse proxy |
| `FIND_CACHE_MAX_BYTES` | `20971520` | Limit rozmiaru cache wyszukiwań (bajty) |
| `ROUTE_CACHE_MAX_BYTES` | `25165824` | Limit rozmiaru cache tras (bajty) |
| `LOG_LEVEL` | `INFO` | Poziom logowania (`LOG_FILE` — opcjonalna ścieżka pliku) |

> **⚠️ Uwaga:** `server.env` zawiera szczegóły Twojego serwera i **nigdy nie powinien trafić do repo** — jest w `.gitignore`.

### Bezpieczeństwo aktualizacji

Skrypty **nie nadpisują** własnych plików ani konfiguracji serwera podczas aktualizacji z GitHub. Chronione pliki to: `autoupdate.sh`, `restart.sh`, `common.sh`, `server.env`, `server.env.example`, `start.sh`, `stop.sh`, `generate-assets.sh`, `preview-logo.sh`, logi, `venv/` i `data/`.

## 🛡️ Bezpieczeństwo

- **Rate limiting** per IP (osobne limity dla statycznych i kosztownych żądań)
- **Limit równoczesnych żądań** (20) — ochrona przed przeciążeniem
- **Blokada niebezpiecznych ścieżek** (`.env`, `.git`, `wp-admin`, `phpmyadmin` itd.)
- **Nagłówki bezpieczeństwa** (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`)
- **Escape HTML** — ochrona przed XSS
- **Walidacja wejścia** (długość, format, NaN/Infinity)
- **Wykrywanie botów** — serwowanie dedykowanych OG meta dla crawlerów

## 🎨 Logo i asset'y

Logo aplikacji znajduje się w `public/logo.svg` i jest **jedynym źródłem prawdy**. Aby zmiana logo propagowała się do faviconu i statycznego obrazu OG, uruchom:

```bash
bash generate-assets.sh
```

Skrypt regeneruje:
- `public/favicon.svg` — favicon z logo osadzonym inline
- `public/og-image.svg` — statyczny obraz OG z logo osadzonym inline

Dynamiczny obraz OG (`/api/og-image`) czyta `logo.svg` w czasie działania serwera, więc aktualizuje się automatycznie bez uruchamiania skryptu.

Aby wygenerować podglądy wszystkich assetów (w tym dynamicznego OG), uruchom:

```bash
bash preview-logo.sh
```

Wynik trafia do katalogu `previews/`.

## 📁 Struktura projektu

```
├── server.py              # Punkt wejścia (uruchamia serwer)
├── server/                # Pakiet: data, cost, handler, pathfinding (stdlib only)
├── process_gtfs.py        # Przetwarzanie GTFS → JSON
├── pricing.json           # Konfiguracja cen
├── autoupdate.sh          # Auto-aktualizacja i self-recovery
├── restart.sh             # Restart serwera
├── common.sh              # Współdzielone funkcje skryptów
├── generate-assets.sh     # Generowanie favicon/og-image z logo
├── preview-logo.sh        # Generowanie podglądów assetów
├── server.env.example     # Szablon konfiguracji serwera
├── public/                # Frontend (HTML/CSS/JS, logo, favicon)
│   └── js/route.js        # Logika tras i mapy
├── processed/             # Przetworzone dane (JSON)
├── data/                  # Surowe dane GTFS
└── previews/              # Podglądy assetów
```
## 🧪 Testy

Opis testów (sposób uruchamiania i pokrycie) znajduje się w [`tests/README.md`](tests/README.md).
