# Za Ile Przejadę? 🚌🚊

Kalkulator cen biletów MPK Kraków 2027 - oblicz koszt podróży komunikacją miejską w Krakowie w oparciu o nowy system biletów opartych na odległości.

## Funkcjonalności

- Wyszukiwanie przystanków po nazwie
- Wybór przystanków na mapie
- Obliczanie dystansu i kosztu przejazdu
- Dwa tryby nawigacji: krótka trasa (najkrótszy dystans) i wygodna trasa (najmniej przesiadek)
- Wyświetlanie trasy na mapie

## Dane

Dane linii i przystanków pochodzą z rozkładów GTFS MPK Kraków.

## Uruchomienie lokalne

```bash
python server.py
```

Serwer uruchomi się na `http://localhost:8080`.

## Logo i asset'y

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

## Deployment

Aplikacja jest gotowa do deploymentu na [Render](https://render.com).
Wystarczy połączyć repozytorium GitHub i wybrać "Deploy from Git".

## Skrypty serwera (auto-update i self-recovery)

Repozytorium zawiera skrypty do utrzymania serwera w ruchu:

- **`autoupdate.sh`** — sprawdza co 2 minuty (przez cron) czy na GitHubie są nowe commity, weryfikuje spójność danych i zdrowie serwera. W razie potrzeby aktualizuje kod i restartuje serwer.
- **`restart.sh`** — zatrzymuje i uruchamia serwer ponownie.
- **`common.sh`** — współdzielone funkcje używane przez powyższe skrypty.

### Konfiguracja

Skrypty czytają konfigurację z pliku `server.env` (ignorowanego przez git). Skopiuj szablon i dostosuj:

```bash
cp server.env.example server.env
```

Dostępne zmienne (wszystkie opcjonalne):
- `PORT` — port serwera (domyślnie `8080`)
- `HEALTH_URL` — URL do sprawdzania zdrowia (domyślnie `http://127.0.0.1:$PORT/api/health`)
- `GIT_BRANCH` — gałąź git do śledzenia (domyślnie bieżąca)
- `RUN_IN_BACKGROUND` — czy uruchamiać serwer w tle (`true`/`false`)

> **Uwaga:** `server.env` zawiera szczegóły Twojego serwera (np. port) i **nigdy nie powinien trafić do repo** — jest w `.gitignore`.

### Cron (co 2 minuty)

```cron
*/2 * * * * /ścieżka/do/autoupdate.sh
```

### Bezpieczeństwo aktualizacji

Skrypty **nie nadpisują** własnych plików ani konfiguracji serwera podczas aktualizacji z GitHub. Chronione pliki to: `autoupdate.sh`, `restart.sh`, `common.sh`, `server.env`, `server.env.example`, `start.sh`, `stop.sh`, `generate-assets.sh`, `preview-logo.sh`, logi, `venv/` i `data/`.

## Technologie

- Python (tylko biblioteka standardowa)
- HTML/CSS/JavaScript
- Leaflet.js (mapy)