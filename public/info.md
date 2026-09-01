# Garść faktów

Żeby lepiej zrozumieć o co chodzi i dlaczego taki kalkulator powstał:

Zgodnie z oficjalnym komunikatem ([źródło](https://www.facebook.com/photo/?fbid=1445327380951786&set=a.612767970874402)):

Od 2027 roku bilety komunikacji miejskiej w Krakowie będziemy liczyć na odległość.

## Jak to będzie działać?

Według mojej interpretacji:

👉 **Płacisz za to, ile kilometrów faktycznie przejedziesz.** Nie za czas, nie za liczbę przystanków — tylko za dystans.

## Nowy cennik

- ➡️ Do 3,5 km: **4 zł** normalny / **2 zł** ulgowy
- ➡️ Powyżej 3,5 km: każde rozpoczęte 500 m to **+0,50 zł** / **+0,25 zł**
- ➡️ Maksymalnie zapłacisz **9 zł** / **4,50 zł** za jeden przejazd

## Jak liczymy przesiadki?

Każdy **przejazd** (od wejścia do pojazdu do przesiadki) to **osobny bilet** liczony od zera. Czyli jeśli jedziesz z przesiadką, płacisz za każdy przejazd osobno — nie sumujemy dystansu całej trasy.

Przykład: jedziesz 2 km tramwajem (4 zł), przesiadasz się i jedziesz 1 km autobusem (4 zł). Razem: **8 zł**, a nie 4 zł za całą trasę.

## Czas przejazdu

Aplikacja pokazuje też **szacunkowy czas przejazdu** (z czasem przesiadki pięciu minut). Czas liczony jest na podstawie oficjalnych rozkładów jazdy (GTFS) — to suma czasów przejazdu między przystankami na wybranej trasie.

## Limit dzienny

Jest też plus — dzienny limit wydatków **20 zł** normalny / **10 zł** ulgowy. Po osiągnięciu tej kwoty kolejne przejazdy tego dnia są **gratis**, tak jakbyś miał bilet dzienny. Przy przesiadkach szybciej dobijasz do limitu.

## Dwa warianty trasy

Aplikacja pokazuje **dwie trasy** i obie mają **gwarancję najniższej możliwej ceny** w tym modelu (algorytm sprawdza wszystkie sensowne kombinacje przejazdów):

- 💰 **Tania trasa** — najtańsze łączne bilety,
- 🛋️ **Wygodna trasa** — mniej przesiadek (każde wsiadanie „kosztuje" w cenniku algorytmu 2 zł, więc algorytm balansuje cenę i wygodę).

---

⚠️ Sytuacja i powyższe stwierdzenia mogą zmienić się w każdej chwili. Sprawdzaj oficjalne komunikaty!

**Koniecznie przeczytaj "Uwaga"!**