# ⚠️ Ważna sprawa!

Hej, dzięki, że tu jesteś!

Ta aplikacja to **NIE jest oficjalne narzędzie ZTP (Zarządu Transportu Publicznego) ani MPK S.A. w Krakowie.** Nie mam z nimi nic wspólnego, ani z urzędem miasta Kraków ani z żadnym innym organem miasta. Lubię wizualizować dane i tak zrobiłem też z nadchodzącymi cenami biletów, żeby zobaczyć jak te nowe ceny będą wyglądać w praktyce.

## Co musisz wiedzieć?

📏 **Dystanse** które tu widzisz są obliczane na podstawie danych GTFS publikowanych przez ZTP (wersja {{GTFS_VERSION}}, rozkłady ważne {{GTFS_DATES}}) i pokazują przystanki, linie i trasy które tego dnia były zapisane w bazie jako funkcjonujące (będę te dane aktualizować, gdy ZTP opublikuje nowy rozkład). To są dane rozkładowe, więc rzeczywista trasa autobusu czy tramwaju może się różnić. Przy wyborze przystanków na mapie pozycje są **uśrednione** (żeby łatwiej trafić w przystanek, nie peron), ale wyświetlana trasa pokazuje już **rzeczywiste lokalizacje peronów**. Dystanse przejazdów liczone są wzdłuż **podanej trasy linii** (z GTFS `shape_dist_traveled`), które nie zawsze są dokładne. Wyliczone ceny traktuj jako poglądowe — mogą różnić się od wartości, które ostatecznie pokaże oficjalny system mKraków.

💰 **Tryb "Tania trasa"** znajduje przejazd o najniższym łącznym koszcie biletów (każdy przejazd między przesiadkami to osobny bilet), co nie zawsze oznacza najkrótszą trasę. **Tryb "Wygodna trasa"** szuka kompromisu: mniej przesiadek bez nieuzasadnionych objazdów. Oba warianty mają gwarancję najniższej możliwej ceny w tym modelu — aplikacja nie zgaduje, tylko sprawdza wszystkie sensowne kombinacje przejazdów.

🔀 **Dwie interpretacje ceny:** ponieważ miasto nie opisało jeszcze zasad rozliczania przesiadek, każdy wynik pokazujemy w dwóch wariantach — **Interpretacja A** (każdy przejazd między przesiadkami to osobny bilet liczony od zera; cena to suma wszystkich przejazdów) oraz **Interpretacja B** (cała podróż jako jeden bilet — cena za łączny dystans trasy). Trasy są wyszukiwane pod kątem Interpretacji A, a cena B liczona jest dla tej samej znalezionej trasy.

❌ **Nie sugeruj się** tymi wycenami przy planowaniu budżetu — miasto zapowiada obsługę taryfy m.in. w aplikacji mKraków i to jej wskazania będą wiążące.

📢 Czytaj oficjalne komunikaty na stronie ZTP Kraków lub w aplikacji mKraków.

---

Dzięki za przeczytanie!