# Za Ile Przejadę? 🚌🚊

Kalkulator kosztu podróży komunikacją miejską w Krakowie w oparciu o nowy system biletów opartych na odległości.

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

## Deployment

Aplikacja jest gotowa do deploymentu na [Render](https://render.com).
Wystarczy połączyć repozytorium GitHub i wybrać "Deploy from Git".

## Technologie

- Python (tylko biblioteka standardowa)
- HTML/CSS/JavaScript
- Leaflet.js (mapy)