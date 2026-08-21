# Testy

Testy weryfikują logikę wyliczania cen oraz wyszukiwania tras (algorytm A*) i działanie endpointów HTTP.

## Uruchamianie

Z poziomu katalogu głównego repozytorium:

```bash
python -m unittest discover -s tests -v
```

Wymagania:
- Python 3.8+ (tylko biblioteka standardowa).
- Katalog `processed/` z wygenerowanymi danymi GTFS (`python process_gtfs.py`). Testy
  integracyjne ładują rzeczywiste przystanki, trasy i krawędzie grafu.

## Struktura

- `test_cost.py` — jednostkowe testy modułu `server.cost`:
  - `calculate_cost(distance)` — koszt pojedynczego biletu (baza + segmenty co 0,5 km,
    limit pojedynczego biletu 9/4,50 zł, zaokrąglanie w górę częściowego segmentu,
    dystans ≤ 0 → 0).
  - `calculate_route_cost(segments)` — suma kosztów segmentów z limitem dziennym
    20/10 zł; pusty zestaw lub brak `distance` → 0.

- `test_pathfinding.py` — jednostkowe (haversine) i integracyjne testy `server.pathfinding`:
  - poprawność i symetria `haversine_km`;
  - `find_shortest_path` zwraca `(result, error)`;
  - `find_route_between_groups` zwraca trójkę `(short, convenient, cheap)` dla
    `mode='both'` i parę dla pojedynczego trybu;
  - **tania trasa nigdy nie jest droższa niż krótka**
    (`cheap.cost_regular <= short.cost_regular`);
  - trasa do tego samego przystanka → dystans 0;
  - **unikalność grup przystanków** na trasie i pozycje rzeczywistych peronów;
  - każdy segment niesie `stop_positions`, a dystans segmentu = suma rzeczywistych
    krawędzi GTFS;
  - nieistniejąca grupa → błąd.

- `test_handler.py` — end-to-end HTTP: podnosi prawdziwy `ThreadedHTTPServer` na
  losowym porcie i sprawdza:
  - `/api/stops/search` (tablica wyników, zapytania < 2 znaki, wyszukiwanie po kodzie);
  - `/api/cost` (poprawne i błędne parametry);
  - kompresja gzip + nagłówek `Vary`;
  - brak rate-limitu na tanich endpointach (regresja „pusta mapa");
  - `/api/og-image` (SVG, tryby, nieistniejące przystanki);
  - `/api/find-route` (wszystkie tryby, tania ≤ krótka, `group_id` na trasie,
    błąd dla złych grup);
  - `/api/health`, `/api/status` (metryki);
  - cykl życia `/api/route-viz` / `/api/route-progress`.

## Uwagi

- Testy integracyjne korzystają z realnych danych w `processed/`; po zmianie GTFS
  uruchom `python process_gtfs.py`, by je odświeżyć.
- Nie wymagają sieci ani zewnętrznych zależności.
