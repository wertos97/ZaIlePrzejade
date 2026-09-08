# Garść faktów

Żeby lepiej zrozumieć o co chodzi i dlaczego taki kalkulator powstał:

Od stycznia 2027 r. Kraków planuje uruchomić dodatkową, **opcjonalną taryfę odległościową**, działającą **obok** obecnych biletów czasowych, jednorazowych i okresowych. Pasażer sam wybierze, z którego rozliczenia skorzysta.

Rada Miasta Krakowa przyjęła uchwałę w sprawie taryfy w grudniu 2025 r., a w lipcu 2026 r. ZTP ogłosił przetarg na system do jej obsługi. Planowany start to styczeń 2027 r. ([źródło: oficjalny komunikat ZTP z 23.07.2026](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/ogloszenie-przetargu-na-wprowadzenie-odleglosciowej-taryfy-biletowej.html)).

## Co potwierdziło miasto

👉 **Płacisz za to, ile kilometrów faktycznie przejedziesz.** Opłata zależy od dystansu pokonanego przez pasażera, a nie od czasu podróży, korków czy liczby przystanków.

## Nowy cennik

- ➡️ Do 3,5 km: **4 zł** normalny / **2 zł** ulgowy
- ➡️ Powyżej 3,5 km: każde rozpoczęte 500 m to **+0,50 zł** / **+0,25 zł**
- ➡️ Maksymalnie zapłacisz **9 zł** / **4,50 zł** za jeden przejazd
- ➡️ Limit 24-godzinny: **20 zł** / **10 zł** — według obecnych założeń miasta po jego osiągnięciu kolejne przejazdy przez 24 godziny od rozpoczęcia pierwszej podróży nie będą dodatkowo płatne

## Przesiadki: co zakłada kalkulator

**Przesiadki: sposób naliczania nie został jeszcze ostatecznie opisany przez miasto.** ZTP podaje, że pasażer zarejestruje w aplikacji mKraków wejście i wyjście z pojazdu, a podróże z przesiadkami system ma uwzględnić w kolejnym etapie. Szczegółowych zasad rozliczania przesiadek jeszcze nie opublikowano.

**Kalkulator obecnie zakłada osobne naliczanie każdego przejazdu pojazdem** — każdy przejazd (od wejścia do pojazdu do przesiadki) to osobny bilet liczony od zera.

Przykład (Interpretacja A — założenie kalkulatora, nie oficjalna zasada): jedziesz 2 km tramwajem (4 zł), przesiadasz się i jedziesz 1 km autobusem (4 zł). Razem: **8 zł**, a nie 4 zł za całą trasę.

W wynikach pokazujemy też **Interpretację B**: całą podróż jako jeden bilet za łączny dystans (w tym przykładzie 3 km, czyli 4 zł). Szczegóły w „Uwaga".

## Czas przejazdu

Aplikacja pokazuje też **szacunkowy czas przejazdu** (z czasem przesiadki pięciu minut). Czas liczony jest na podstawie oficjalnych rozkładów jazdy (GTFS, wersja {{GTFS_VERSION}}) — to suma czasów przejazdu między przystankami na wybranej trasie.

## Dwa warianty trasy

Aplikacja pokazuje **dwie trasy** i obie mają **gwarancję najniższej możliwej ceny** w modelu przyjętym przez kalkulator (Interpretacja A — przy powyższym założeniu o przesiadkach; algorytm sprawdza wszystkie sensowne kombinacje przejazdów):

- 💰 **Tania trasa** — najtańsze łączne bilety,
- 🛋️ **Wygodna trasa** — mniej przesiadek (każde wsiadanie „kosztuje" w cenniku algorytmu 2 zł, więc algorytm balansuje cenę i wygodę).

---

⚠️ Sytuacja i powyższe stwierdzenia mogą zmienić się w każdej chwili. Sprawdzaj oficjalne komunikaty!

**Koniecznie przeczytaj "Uwaga"!**
