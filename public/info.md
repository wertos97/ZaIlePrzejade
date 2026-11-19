# Garść faktów

Żeby lepiej zrozumieć o co chodzi i dlaczego taki kalkulator powstał:

Kraków planuje uruchomić **opcjonalną taryfę odległościową** (wcześniej nazywaną też dynamiczną), działającą **obok** obecnych biletów czasowych, jednorazowych i okresowych. Pasażer sam wybierze, z którego rozliczenia skorzysta. Podstawą jest uchwała Rady Miasta Krakowa z **17 grudnia 2025 r.**; przepisy taryfy odległościowej mają wejść w życie **4 stycznia 2027 r.** (Zarządzenie nr 425/2026 Prezydenta Miasta Krakowa).

**Stan na 19 listopada 2026 r.:** ZTP wybrał wykonawcę systemu (Mera-Serwis SGL, 11.09.2026), ale do Krajowej Izby Odwoławczej wpłynęło odwołanie konkurencyjnej firmy Beesset (sprawa KIO 4697/26, rozprawa 14.10.2026). Na 19.10.2026 nie było publicznego rozstrzygnięcia, a ostatnie komunikaty nie potwierdzają podpisania umowy. Formalnie przetarg nie jest więc zakończony i **start w styczniu 2027 r. nie jest pewny**. Dodatkowo nowy prezydent Krakowa zapowiada najpierw przywrócenie biletu 20-minutowego; nie zapowiada rezygnacji z taryfy odległościowej, ale harmonogram może się zmienić. ([ZTP, 23.09.2026](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/taryfa-odleglosciowa-w-kmk-wybrano-wykonawce-wplynelo-odwolanie-do-kio.html); [Głos24, 19.10.2026](https://glos24.pl/bilet-za-kilometry-w-krakowie-ztp-wyjasnia-jak-to-ma-dzialac))

## Co potwierdziło miasto

👉 **Opłata zależy od odległości.** Zgodnie z tabelą w załączniku do uchwały/Zarządzenia 425/2026 przedziały kilometrowe są liczone **w linii prostej**. Cena przejazdu między tymi samymi przystankami ma być stała, niezależnie od objazdu czy korków. ([ZTP, 23.07.2026](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/ogloszenie-przetargu-na-wprowadzenie-odleglosciowej-taryfy-biletowej.html); [Głos24, 19.10.2026](https://glos24.pl/bilet-za-kilometry-w-krakowie-ztp-wyjasnia-jak-to-ma-dzialac))

👉 **Kalkulator liczy dystans tak jak oficjalna taryfa — w linii prostej.** Dla każdego przejazdu bierzemy odległość między przystankiem wsiadania i wysiadania, więc cena nie zależy od objazdu ani korków. Dystans wzdłuż trasy GTFS pokazujemy osobno jako informację. To nadal model: oficjalny system może zaokrąglać odległość w inny sposób, dlatego ceny traktuj jako szacunek.

## Nowy cennik

- ➡️ Do 3,5 km: **4 zł** normalny / **2 zł** ulgowy
- ➡️ Powyżej 3,5 km: każde rozpoczęte 500 m to **+0,50 zł** / **+0,25 zł**
- ➡️ Maksymalnie **9 zł** / **4,50 zł** za jeden przejazd
- ➡️ Limit 24-godzinny: **20 zł** / **10 zł** — liczony nie od północy, tylko przez 24 godziny od rozpoczęcia pierwszej podróży; po osiągnięciu limitu kolejne przejazdy w tym okresie są bez dodatkowych opłat. Kalkulator wycenia jedną podróż; nie śledzi Twoich przejazdów przez całą dobę.

## Przesiadki: co wiemy

**W podstawowej wersji systemu każdy pojazd to osobny, płatny przejazd.** ZTP zapowiada, że pełne łączenie przejazdów z przesiadkami ma pojawić się dopiero **około sześciu miesięcy po wdrożeniu** podstawowej wersji; do tego czasu nawet przesiadka wymuszona awarią oznacza drugi bilet. Szczegóły późniejszego łączenia (czas, odległość, powrót) nie są opublikowane. ([Głos24, 19.10.2026](https://glos24.pl/bilet-za-kilometry-w-krakowie-ztp-wyjasnia-jak-to-ma-dzialac); [ZTP, 23.07.2026](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/ogloszenie-przetargu-na-wprowadzenie-odleglosciowej-taryfy-biletowej.html))

**Kalkulator pokazuje wyłącznie aktualny, pierwszy etap systemu:** każdy przejazd pojazdem to osobny bilet liczony od zera. Dystans każdego przejazdu to linia prosta **A→B** między przystankiem wsiadania i wysiadania — nie długość trasy i nie suma odcinków między kolejnymi przystankami. Po włączeniu łączenia przesiadek (około sześciu miesięcy później) zasady mają się zmienić, ale szczegóły nie są opublikowane; nie pokazujemy hipotetycznych wariantów.

Przykład: 2 km tramwajem (4 zł) + 1 km autobusem (4 zł) = **8 zł** — każdy pojazd osobno, każdy dystans A→B liczony od zera.

## Jak ma działać oficjalny system

- Taryfa ma działać **wyłącznie przez aplikacje mobilne** (mKraków lub inne aplikacje po umowie z ZTP), nie przez Krakowską Kartę Miejską czy samą kartę płatniczą.
- Pasażer **rozpoczyna i kończy przejazd w aplikacji**; przy rozpoczęciu podaje numer pojazdu. Podczas kontroli aplikacja pokazuje kod QR.
- W mKraków przed startem ma być widoczna **orientacyjna cena**.
- Jeśli zapomnisz zakończyć przejazd, opłata może zostać naliczona do **ostatniego przystanku linii** (maks. 9 zł / 4,50 zł).
- Cena między tymi samymi przystankami ma być stała; objazd nie powinien podnosić opłaty.

([Głos24, 19.10.2026](https://glos24.pl/bilet-za-kilometry-w-krakowie-ztp-wyjasnia-jak-to-ma-dzialac))

## Czas przejazdu

Aplikacja pokazuje też **szacunkowy czas przejazdu** (z czasem przesiadki pięciu minut). Czas liczony jest na podstawie oficjalnych rozkładów jazdy (GTFS, wersja {{GTFS_VERSION}}) — to suma czasów przejazdu między przystankami na wybranej trasie. To szacunek z rozkładu, nie czas rzeczywisty.

## Dwa warianty trasy

Aplikacja pokazuje **dwie trasy** i obie mają **gwarancję najniższej możliwej ceny w modelu kalkulatora** (oficjalny pierwszy etap — osobny bilet za każdy przejazd; algorytm sprawdza wszystkie sensowne kombinacje przejazdów):

- 💰 **Tania trasa** — najtańsze łączne bilety,
- 🛋️ **Wygodna trasa** — mniej przesiadek (każde wsiadanie „kosztuje" w cenniku algorytmu 2 zł, więc algorytm balansuje cenę i wygodę).

Gwarancja dotyczy modelu kalkulatora (oficjalna linia prosta A→B za każdy przejazd, pierwszy etap), nie oficjalnego systemu — ten może stosować inne zaokrąglenia i przez pierwsze miesiące rozlicza osobno każdy pojazd.

---

⚠️ Sytuacja i powyższe stwierdzenia mogą zmienić się w każdej chwili. Sprawdzaj oficjalne komunikaty!

**Koniecznie przeczytaj "Uwaga"!**

## Źródła

- [ZTP: ogłoszenie przetargu i zasady taryfy (23.07.2026)](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/ogloszenie-przetargu-na-wprowadzenie-odleglosciowej-taryfy-biletowej.html)
- [ZTP: wybór wykonawcy i odwołanie do KIO (23.09.2026)](https://ztp.krakow.pl/wszystkie-aktualnosci/kmk/taryfa-odleglosciowa-w-kmk-wybrano-wykonawce-wplynelo-odwolanie-do-kio.html)
- [Miasto Kraków: nowe ceny od 2.03.2026 i taryfa 2027 (2.02.2026)](https://krakow.pl/aktualnosci/301273,1912,komunikat,od_2_marca_nowe_ceny_biletow_komunikacji_miejskiej_w_krakowie.html)
- [Miasto Kraków: odwołanie do KIO (28.09.2026)](https://krakow.pl/aktualnosci/338735,1912,komunikat,taryfa_odleglosciowa_w_kmk__po_wyborze_wykonawcy_wplynelo_odwolanie_do_kio.html)
- [Głos24: ZTP wyjaśnia zasady — aplikacje, przesiadki, start (19.10.2026)](https://glos24.pl/bilet-za-kilometry-w-krakowie-ztp-wyjasnia-jak-to-ma-dzialac)
- [Zarządzenie nr 425/2026 — załącznik taryfowy: przedziały w linii prostej](https://www.kk.krakow.pl/aktyprawne/taryfa-biletowa/download,43.html)
- [TransInfo: system od 4.01.2027, wybór Mera-Serwis (14.09.2026)](https://transinfo.pl/infotrans/mera-bierze-kolejny-kontrakt-w-krakowie-i-za-prawie-6-mln-zl-zrobi-taryfe-odleglosciowa/)
