# 🔒 Prywatność

Krótko: **nie musisz zakładać konta, nie ma tu reklam i nie ma zewnętrznej analityki.** Strona jest dostarczana przez Cloudflare (CDN/ochrona) i nasz serwer; przeglądarka nie łączy się z zewnętrznymi reklamami ani narzędziami śledzącymi.

## Czego tu nie ma

- ❌ kont i logowania dla odwiedzających,
- ❌ reklam,
- ❌ narzędzi śledzących (takich jak Google Analytics),
- ❌ profili użytkowników,
- ❌ ciasteczek reklamowych ani analitycznych.

## Ciasteczka

- Nasza strona **nie ustawia ciasteczek** poza opcjonalnym `operator=1` (tylko operator na własnym urządzeniu; opis niżej).
- Cloudflare jako dostawca CDN/ochrony może ustawić **niezbędne ciasteczka bezpieczeństwa** (np. przy weryfikacji ruchu). Nie używamy ich do śledzenia.
- Nie ma ciasteczek reklamowych ani analitycznych.

## Cloudflare

Strona jest za Cloudflare (CDN/ochrona, tunel do naszego serwera). Cloudflare widzi techniczne dane połączenia (adres IP, nagłówki, czas) i przetwarza je zgodnie z własną polityką: [cloudflare.com/privacypolicy](https://www.cloudflare.com/privacypolicy/). Nasz serwer dostaje adres IP w nagłówku `CF-Connecting-IP` tylko po to, żeby działał limit zapytań i solony skrót w statystykach; **nie zapisujemy surowego adresu IP**.

## Co zapisujemy i po co

- 🗺️ **Mapa:** obrazki mapy przesyłamy Ci z naszego serwera (proxy), więc firma dostarczająca mapę (CARTO) **nie widzi, że tu jesteś**. Cloudflare widzi samo połączenie.
- 📊 **Liczniki odwiedzin:** zapisujemy, ile osób weszło na stronę i ile tras policzono — przy wyszukiwaniach także między którymi przystankami, ile trwało liczenie i ile wyszło (żeby wyłapywać błędy). Nie zapisujemy Twojego adresu internetowego — tylko jego **solony skrót**, po którym nie da się dojść, kto to był. Surowe zdarzenia kasują się same **po 90 dniach**; dzienne podsumowania bez identyfikatorów zostają bezterminowo. Własny ruch operatora strony jest zapisywany z oznaczeniem i domyślnie ukrywany w jego panelu (może go podejrzeć przełącznikiem) — Ciebie to nie dotyczy.
- 🛡️ **Ochrona przed przeciążeniem:** żeby jedna osoba nie zapchała strony innym, serwer na chwilę zapamiętuje, skąd przyszło zapytanie — i zaraz o tym zapomina. To dane w pamięci, nie w bazie.
- 👤 **Operator:** opcjonalne ciasteczko `operator=1` ustawia tylko operator na własnym urządzeniu; jego ruch jest zapisywany jako operatorski i domyślnie ukryty w statystykach.

## Twoje prawa

Masz prawo do informacji, dostępu i usunięcia danych. Ponieważ nie prowadzimy kont ani profili, a adresy IP są przechowywane wyłącznie jako nieodwracalne skróty, nie jesteśmy w stanie przypisać ich do konkretnej osoby. W sprawach prywatności napisz przez GitHub (issues) — link do repozytorium jest w sekcji „Autor”.
