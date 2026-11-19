# ⚠️ Ważna sprawa!

Hej, dzięki, że tu jesteś!

Ta aplikacja to **NIE jest oficjalne narzędzie ZTP (Zarządu Transportu Publicznego) ani MPK S.A. w Krakowie.** Nie mam z nimi nic wspólnego, ani z urzędem miasta Kraków ani z żadnym innym organem miasta. **Nie sprzedaję biletów, nie rejestruję przejazdów i nie jestem powiązany z aplikacją mKraków.** Lubię wizualizować dane i tak zrobiłem też z nadchodzącymi cenami biletów, żeby zobaczyć, jak te nowe ceny będą wyglądać w praktyce.

## Co musisz wiedzieć?

📏 **Dystanse** które tu widzisz są obliczane na podstawie danych GTFS publikowanych przez ZTP (wersja {{GTFS_VERSION}}, rozkłady ważne {{GTFS_DATES}}) i pokazują przystanki, linie i trasy, które tego dnia były zapisane w bazie jako funkcjonujące (będę te dane aktualizować, gdy ZTP opublikuje nowy rozkład). To są dane rozkładowe, więc rzeczywista trasa autobusu czy tramwaju może się różnić. Przy wyborze przystanków na mapie pozycje są **uśrednione** (żeby łatwiej trafić w przystanek, nie peron), ale wyświetlana trasa pokazuje już **rzeczywiste lokalizacje peronów**.

**Oficjalna taryfa liczy odległość w linii prostej** (tak wynika z tabeli w załączniku do uchwały/Zarządzenia 425/2026), a cena między tymi samymi przystankami ma być stała, niezależnie od objazdu. **Kalkulator liczy dystans wzdłuż trasy GTFS** (`shape_dist_traveled`), która bywa dłuższa niż linia prosta — dlatego pokazana cena może być **zawyżona** względem oficjalnego systemu. Wyliczone ceny traktuj jako poglądowe — mogą różnić się od wartości, które ostatecznie pokaże oficjalny system mKraków.

💰 **Tryb "Tania trasa"** znajduje przejazd o najniższym łącznym koszcie biletów (każdy przejazd między przesiadkami to osobny bilet), co nie zawsze oznacza najkrótszą trasę. **Tryb "Wygodna trasa"** szuka kompromisu: mniej przesiadek bez nieuzasadnionych objazdów. Oba warianty mają gwarancję najniższej możliwej ceny **w modelu kalkulatora** — aplikacja nie zgaduje, tylko sprawdza wszystkie sensowne kombinacje przejazdów. To nie jest gwarancja ceny w oficjalnym systemie.

🔀 **Dwie interpretacje ceny:** w **podstawowej wersji** systemu (pierwsze około sześciu miesięcy) **każdy pojazd to osobny, płatny przejazd** — to potwierdzone przez ZTP. **Interpretacja A** odpowiada temu etapowi. Później ma pojawić się łączenie przejazdów z przesiadkami; szczegóły (czas, odległość, powrót) nie są opublikowane. **Interpretacja B** to hipotetyczna wersja przyszła: cała podróż jako jeden bilet za łączny dystans. Trasy są wyszukiwane pod A, a cena B liczona jest dla tej samej znalezionej trasy.

📱 **Oficjalny system ma działać wyłącznie przez aplikacje mobilne** (mKraków lub inne aplikacje po umowie z ZTP). Pasażer podaje numer pojazdu, rozpoczyna i kończy przejazd w aplikacji; przy kontroli aplikacja pokazuje kod QR. Jeśli nie zakończysz przejazdu, opłata może zostać naliczona do ostatniego przystanku linii (maks. 9 zł / 4,50 zł). Ta strona nie jest systemem biletowym i nie zastąpi mKraków.

⏳ **Start w styczniu 2027 r. (przepisy: 4.01.2027) nie jest pewny.** Przetarg na system nie jest formalnie zakończony: wpłynęło odwołanie do KIO (sprawa 4697/26; rozprawa 14.10.2026; na 19.10.2026 brak publicznego rozstrzygnięcia). Miasto zmieniło prezydenta; nowy prezydent zapowiada najpierw przywrócenie biletu 20-minutowego, choć nie zapowiada rezygnacji z taryfy odległościowej. Termin może się przesunąć — sprawdzaj oficjalne komunikaty.

❌ **Nie sugeruj się** tymi wycenami przy planowaniu budżetu — miasto zapowiada obsługę taryfy m.in. w aplikacji mKraków i to jej wskazania będą wiążące.

📢 Czytaj oficjalne komunikaty na stronie ZTP Kraków lub w aplikacji mKraków. Więcej faktów i źródła znajdziesz w „Info".

---

Dzięki za przeczytanie!
