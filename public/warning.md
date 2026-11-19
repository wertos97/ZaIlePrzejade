# ⚠️ Ważna sprawa!

Hej, dzięki, że tu jesteś!

Ta aplikacja to **NIE jest oficjalne narzędzie ZTP (Zarządu Transportu Publicznego) ani MPK S.A. w Krakowie.** Nie mam z nimi nic wspólnego, ani z urzędem miasta Kraków ani z żadnym innym organem miasta. **Nie sprzedaję biletów, nie rejestruję przejazdów i nie jestem powiązany z aplikacją mKraków.** Lubię wizualizować dane i tak zrobiłem też z nadchodzącymi cenami biletów, żeby zobaczyć, jak te nowe ceny będą wyglądać w praktyce.

## Co musisz wiedzieć?

📏 **Trasy, przystanki i linie** pochodzą z danych GTFS publikowanych przez ZTP (wersja {{GTFS_VERSION}}, rozkłady ważne {{GTFS_DATES}}) i pokazują to, co tego dnia było zapisane w bazie jako funkcjonujące (dane aktualizuję, gdy ZTP opublikuje nowy rozkład). To dane rozkładowe, więc rzeczywista trasa autobusu czy tramwaju może się różnić. Przy wyborze przystanków na mapie pozycje są **uśrednione** (żeby łatwiej trafić w przystanek, nie peron), ale wyświetlana trasa pokazuje **rzeczywiste lokalizacje peronów**. Dystans trasy (wzdłuż GTFS) pokazuję osobno jako informację.

**Oficjalna taryfa liczy odległość w linii prostej** (tabela w załączniku do uchwały/Zarządzenia 425/2026), a cena między tymi samymi przystankami ma być stała, niezależnie od objazdu. **Kalkulator liczy tak samo:** dla każdego przejazdu bierze odległość **A→B** w linii prostej między przystankiem wsiadania i wysiadania — nie długość trasy i nie sumę odcinków między kolejnymi przystankami. Dystans wzdłuż trasy GTFS (widoczny osobno jako „Dystans trasy”) jest tylko informacją. Ceny nadal traktuj jako szacunek — oficjalny system może zaokrąglać odległość inaczej, a ostateczne wartości pokaże mKraków.

💰 **Tryb "Tania trasa"** znajduje przejazd o najniższym łącznym koszcie biletów (każdy przejazd między przesiadkami to osobny bilet), co nie zawsze oznacza najkrótszą trasę. **Tryb "Wygodna trasa"** szuka kompromisu: mniej przesiadek bez nieuzasadnionych objazdów. Oba warianty mają gwarancję najniższej możliwej ceny **w modelu kalkulatora** — aplikacja nie zgaduje, tylko sprawdza wszystkie sensowne kombinacje przejazdów. To nie jest gwarancja ceny w oficjalnym systemie.

🔀 **Jeden oficjalny model (pierwszy etap):** w **podstawowej wersji** systemu (pierwsze około sześciu miesięcy) **każdy pojazd to osobny, płatny przejazd** — to potwierdzone przez ZTP. Każdy przejazd jest wyceniany według odległości **A→B w linii prostej** między przystankiem wsiadania i wysiadania. Później ma pojawić się łączenie przejazdów z przesiadkami; szczegóły (czas, odległość, powrót) nie są opublikowane, więc aplikacja nie pokazuje hipotetycznych wariantów przyszłej taryfy.

📱 **Oficjalny system ma działać wyłącznie przez aplikacje mobilne** (mKraków lub inne aplikacje po umowie z ZTP). Pasażer podaje numer pojazdu, rozpoczyna i kończy przejazd w aplikacji; przy kontroli aplikacja pokazuje kod QR. Jeśli nie zakończysz przejazdu, opłata może zostać naliczona do ostatniego przystanku linii (maks. 9 zł / 4,50 zł). Ta strona nie jest systemem biletowym i nie zastąpi mKraków.

⏳ **Start w styczniu 2027 r. (przepisy: 4.01.2027) nie jest pewny.** Przetarg na system nie jest formalnie zakończony: wpłynęło odwołanie do KIO (sprawa 4697/26; rozprawa 14.10.2026; na 19.10.2026 brak publicznego rozstrzygnięcia). Miasto zmieniło prezydenta; nowy prezydent zapowiada najpierw przywrócenie biletu 20-minutowego, choć nie zapowiada rezygnacji z taryfy odległościowej. Termin może się przesunąć — sprawdzaj oficjalne komunikaty.

❌ **Nie sugeruj się** tymi wycenami przy planowaniu budżetu — miasto zapowiada obsługę taryfy m.in. w aplikacji mKraków i to jej wskazania będą wiążące.

📢 Czytaj oficjalne komunikaty na stronie ZTP Kraków lub w aplikacji mKraków. Więcej faktów i źródła znajdziesz w „Info".

---

Dzięki za przeczytanie!
