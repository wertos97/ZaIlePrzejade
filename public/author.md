# Aplikacja "Za Ile Przejadę?"

**Kalkulator planowanej taryfy odległościowej w Krakowie (2027)**

Aplikacja została stworzona w celu obliczania szacunkowych kosztów podróży komunikacją miejską w Krakowie w oparciu o planowaną, opcjonalną taryfę odległościową, która ma wejść w życie w 2027 roku. Start jest jednak uzależniony od zakończenia przetargu na system (odwołanie do KIO) — szczegóły i źródła w „Info”.

Dane linii i przystanków pochodzą z ogólnodostępnych rozkładów GTFS publikowanych przez ZTP Kraków (wersja {{GTFS_VERSION}} — po publikacji nowego rozkładu dane są odświeżane).

Przy wycenie stosuję oficjalną zasadę taryfy: odległość w linii prostej między przystankiem wsiadania i wysiadania każdego przejazdu. Dystans wzdłuż rozkładowej trasy pokazuję dodatkowo jako informację.

---

- **Kod źródłowy:** [github.com/wertos97/ZaIlePrzejade](https://github.com/wertos97/ZaIlePrzejade)
- **Autor:** wertos97
- 🗺️ Mapę ogarnia [Leaflet](https://leafletjs.com/) i dane [OpenStreetMap](https://www.openstreetmap.org/) (kafelki CARTO serwowane przez nasz serwer — Twoja przeglądarka nie łączy się z zewnętrznymi dostawcami map; sam ruch obsługuje Cloudflare jako CDN — szczegóły w „Prywatności").

W przypadku pytań lub uwag zapraszam do kontaktu przez GitHub. Wszelkie issues i PRy mile widziane!

---

Zachęcam do udostępniania linków do tras na social mediach — robią się wtedy automatycznie ładne obrazki, nad którymi długo pracowałem :)

A jeśli aplikacja podoba ci się na tyle, że chcesz mi podziękować, **kup mi bilet!**
[ko-fi.com/wertos97](https://ko-fi.com/wertos97)
