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

## Technologie

- Python (tylko biblioteka standardowa)
- HTML/CSS/JavaScript
- Leaflet.js (mapy)