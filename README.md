<p align="center">
  <img src="public/logo.svg" alt="Za Ile Przejadę?" width="110" />
</p>

<h1 align="center">🚌🚊 Za Ile Przejadę?</h1>

<p align="center">
  <a href="https://img.shields.io/endpoint?url=https%3A%2F%2Fzaileprzeja.de%2Fapi%2Fbadge%2Fversion"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fzaileprzeja.de%2Fapi%2Fbadge%2Fversion" alt="wersja" /></a>
  <a href="https://zaileprzeja.de"><img src="https://img.shields.io/website?url=https%3A%2F%2Fzaileprzeja.de&up_message=online&down_message=offline&label=status" alt="status" /></a>
  <a href="https://github.com/wertos97/ZaIlePrzejade/commits/main"><img src="https://img.shields.io/github/last-commit/wertos97/ZaIlePrzejade/main" alt="ostatni commit" /></a>
</p>

<p align="center">
  <a href="https://zaileprzeja.de"><strong>🌐 zaileprzeja.de</strong></a>
</p>

Kalkulator cen biletów komunikacji miejskiej w Krakowie w taryfie 2027 (opartej na przejechanym dystansie). Aplikacja pokazuje połączenia pomiędzy dwoma wybranymi przystankami w **dwóch wariantach**:

- **Trasa tania** — najtańszy wariant. Każdy przejazd to osobny bilet liczony
  od zera, więc najtańsza trasa nie zawsze jest najkrótsza,
- **Trasa wygodna** — najmniej przesiadek przy rozsądnej cenie.

## OG Image dla sociali

Aplikacja automatycznie generuje obrazki OG w sposób dynamiczny dla danej trasy.

| Typowa cena | Cena na limicie dziennym |
|---|---|
| ![Typowa](previews/og-preview-typical.png) | ![Max](previews/og-preview-max.png) |

## Wyszukiwanie tras

Podstawą jest graf zbudowany z danych GTFS (`process_gtfs.py` →
`processed/`): przystanki z pozycjami, przejazdy linii z realnymi
dystansami (`shape_dist_traveled`) i przejścia piesze między przystankami (peronami).

1. **Cała jazda liczona naraz** — Dijkstra przechodzi po krawędziach jednej linii i wyznacza koszt jazdy daną linią do wszystkich przystanków jednocześnie.
2. **Składanie tras z przejazdów** — trasa to kilka takich jazd połączonych przesiadkami. Aplikacja składa: jazdy ze startu, jazdy do celu i jazdy pośrednie. Te złożenia pokrywają **wszystkie trasy do 4 przejazdów**, a tańsze opcje odcinają droższe, więc typowa para liczy się w ułamkach sekundy.
3. **Gwarancja najlepszej ceny** — 5. przejazd zawsze kosztuje co najmniej taryfę bazową, a łączna cena nie może przekroczyć dziennego limitu. Dlatego najlepsza trasa do 4 przejazdów jest **matematycznie najlepszą możliwą trasą** — i właśnie ją pokazujemy.
4. **Awaryjne A\*** — dla bardzo trudnych par: dokładne wyszukiwanie Pareto z limitami czasu (8 s / 8 s / 10 s przy limicie żądania 30 s). Gdy limit przekroczony — błąd dla trybu, nigdy wynik „na oko".

Dwa tryby różnią się tylko tym, co liczą: **tania** minimalizuje samą
taryfę, **wygodna** taryfę powiększoną o 2,00 zł za każde wsiadanie.

## Cache

Wyniki zapisują się w **dwóch miejscach** i żyją, dopóki nie zmienią się dane GTFS (wersja danych i algorytmu jest w nazwie pliku bazy):

- **dysk (sqlite)** — `processed/route_cache_<feed>_<algo>.sqlite`; każda nowa trasa od razu zapisuje się na dysk, więc **przetrwa restart serwera** i czyszczenie pamięci,
- **pamięć RAM** (25 MB) — najczęściej używane trasy; gdy trasy nie ma w pamięci, wraca z dysku w 1–4 ms.

## Model cen (2027)

| Parametr | Wartość |
|----------|---------|
| Bilet bazowy (do 3,5 km) | 4,00 zł / 2,00 zł (ulgowy) |
| Każde kolejne 0,5 km | +0,50 zł / +0,25 zł |
| Maksymalna cena pojedynczego biletu | 9,00 zł / 4,50 zł |
| **Limit dzienny** | **20,00 zł / 10,00 zł** |

Konfiguracja: [`pricing.json`](pricing.json).

## Uruchomienie

Wymagania: Python 3.8+, zero dodatkowych pakietów.

```bash
git clone https://github.com/wertos97/ZaIlePrzejade.git
cd ZaIlePrzejade
python server.py                # http://localhost:8080
```

Dane GTFS przetwarza `process_gtfs.py` (surowe pliki zip w `data/` →
`processed/*.json`).

## API

| Endpoint | Opis |
|----------|------|
| `GET /api/find-route?from=<id>&to=<id>` | `{convenient, cheap}` — obie trasy |
| `GET /api/stops` / `api/stops/search?q=` | Przystanki / wyszukiwanie |
| `GET /api/cost?distance=<km>` | Koszt dla dystansu |
| `GET /api/og-image?from=<id>&to=<id>&mode=` | Karta OG (SVG) |
| `GET /api/health` · `api/status` · `api/version` | Kondycja serwera |

## Testy

```bash
python -m unittest discover -s tests     # 75 testów
```

W tym `TestExactSearchExactness` — na małym grafie testowym algorytm jest porównywany z metodą, która sprawdza wszystkie możliwe trasy. Wyniki muszą się zgadzać.
