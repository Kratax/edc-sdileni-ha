# EDC sdílení elektřiny — Home Assistant integrace

Neoficiální integrace pro Home Assistant, která pro zadané **výrobní EANy**
stahuje z portálu [portal.edc-cr.cz](https://portal.edc-cr.cz) (EDC —
Elektroenergetické datové centrum) denní data o výrobě a o úspěšném sdílení
elektřiny a vytváří z nich senzory.

Konfiguruje se celá přes UI Home Assistanta (žádné YAML) — přihlašovací
údaje i seznam EAN se dají kdykoliv později upravit.

## Co dostaneš

Každý nastavený EAN je v Home Assistantu samostatné **zařízení** se třemi
entitami:

| Entita | Popis | Jednotka |
|---|---|---|
| Výroba (export) | celková naměřená dodávka za poslední uzavřený den | kWh |
| Úspěšně sdíleno | kolik z dodávky bylo skutečně sdíleno | kWh |
| Podíl sdílené elektřiny | sdíleno / dodávka | % |

Zbytek dodávky (`dodávka − sdíleno`) je to, co portál označuje jako
**„Prodáno obchodníkovi"** — v grafech vystupuje jako „Nesdíleno".

Energetické entity mají v atributu `historie_dni` i celou dosud známou
historii po dnech — hodí se pro vlastní graf (`history-graph`, ApexCharts
karta apod.).

## Jak se to chová

- **Denní aktualizace** v nastavený čas (výchozí 10:30) pro každý EAN
  zvlášť — v tu dobu už bývá předchozí den na portálu vyhodnocený.
- **Backfill při startu HA:** pro každý EAN se zkontroluje, jestli má
  aktuální kalendářní měsíc kompletní data. Pokud ne (nová instalace,
  výpadek, restart po delší odstávce...), integrace sama dotáhne z portálu
  vše chybějící zpětně až od "Začátku historie" (nastavíš v Možnostech,
  typicky datum registrace EAN ke sdílení).
- **Chybějící den na portálu se nevymýšlí:** pokud portál pro konkrétní den
  ještě nemá zpracovaná data, integrace pro něj nic neuloží a zkusí to znovu
  příště — nikdy neuloží nulu tam, kde ve skutečnosti "nevíme".
- **Retry při výpadku:** pokud SSO/API portálu neodpovídá nebo hodí chybu,
  zkusí to znovu za **5 minut**, a pokud to nevyjde ani napodruhé, dál každou
  **hodinu** dokud se spojení neobnoví.
- **Neplatné přihlašovací údaje:** pokud EDC odmítne heslo, integrace to
  nezkouší dokola (zbytečně by riskovala zablokování účtu) a rovnou nabídne
  v UI **"Znovu ověřit" (reauth)** formulář pro zadání nových údajů. Chyba se
  zapíše i do logu.
- **Neexistující/špatný EAN:** pokud portál konkrétní EAN vůbec nezná (překlep,
  není registrovaný ke sdílení...), integrace ho přestane zkoušet stahovat,
  zapíše chybu do logu a založí upozornění v **Nastavení -> Opravy
  (Repairs)**. Pokud jde o jediný nastavený EAN, celá integrace přejde do
  chybového stavu ("Failed to set up"), dokud EAN neopravíš v Možnostech.
  Pokud máš EANů víc a selže jen jeden, ostatní zařízení fungují dál beze
  změny.

## ⚠️ Než začneš — přečti si to

1. **Toto NENÍ oficiální API.** Je to znovuzkonstruované (reverse-engineered)
   interní rozhraní, které portál používá sám pro sebe. EDC výslovně
   neposkytuje automatizovaný přístup fyzickým osobám — API se může kdykoliv
   změnit, přestat fungovat, nebo to může být v rozporu s podmínkami užívání
   portálu. Používáš na vlastní riziko a odpovědnost.
2. **Přihlašovací jméno je e-mail**, kterým se hlásíš na portal.edc-cr.cz.
3. Heslo bude uložené v konfiguraci Home Assistanta (`.storage/core.config_entries`)
   — chraň zálohy HA stejně jako jakékoliv jiné citlivé údaje.
4. **Dvoufaktorové ověření (OTP) integrace neumí.** Pokud ho máš na účtu
   zapnuté, přihlášení skončí chybou, která ti to řekne.

## Jak funguje přihlašování

Portál EDC je OIDC klient (Keycloak) a jeho frontend se hlásí přes
**authorization code + PKCE**. Integrace dělá totéž, jen bez prohlížeče:

1. Zavolá `/protocol/openid-connect/auth` s PKCE challenge a dostane
   přihlašovací stránku Keycloaku.
2. Odešle e-mail a heslo do formuláře `kc-form-login` (zvládne i realmy, které
   se ptají nejdřív na jméno a pak na heslo).
3. Z přesměrování si vezme `code` a vymění ho na token endpointu za tokeny.

Při přihlášení si integrace říká o rozsah **`offline_access`**. Když ho EDC
povolí, dostane *offline* refresh token, který nepřežívá jen SSO session, ale
platí dlouhodobě — od té chvíle se heslo **už nepoužívá vůbec**, integrace jen
obnovuje access token. Refresh token se ukládá do `.storage`, takže ani restart
Home Assistanta nevyžaduje nové přihlášení.

Jeden config entry = jedno přihlášení, které si sdílí všechny nastavené EANy.
Když API odmítne token uprostřed stahování (typicky při dlouhém backfillu),
integrace si ho jednou tiše obnoví a pokračuje.

Pokud by browser flow nešel použít, integrace ještě zkusí klasický
**password grant** (OAuth2 Resource Owner Password Credentials) — nejdřív
s `offline_access`, pak bez něj. Chybu, kterou vrátí Keycloak, uvidíš
v konfiguračním formuláři i v logu, takže je poznat rozdíl mezi špatným heslem,
zablokovaným účtem a nedostupným SSO.

Když Keycloak přihlašovací údaje výslovně odmítne, integrace **nezkouší další
varianty** — opakované pokusy se stejným heslem jen spouštějí brute-force
ochranu a dokážou zablokovat funkční účet.

### Hlavička `Edc-Contract-Type`

Samotný platný token na data nestačí. Backend EDC odpovídá
`403 SECURITY_OPERATION_NOT_ALLOWED`, dokud request neobsahuje hlavičku
`Edc-Contract-Type: STANDARD` — tu posílá i frontend portálu a odpovídá segmentu
`/standard/` v URL API. Integrace ji posílá spolu s `X-Correlation-ID`, takže
její requesty vypadají stejně jako ty z portálu.

## Instalace

### Varianta A — přes HACS (doporučeno)

1. V Home Assistantu otevři **HACS → Integrace → tři tečky vpravo nahoře →
   Vlastní repozitáře (Custom repositories)**.
2. Vlož URL `https://github.com/Kratax/edc-sdileni-ha`, kategorie
   **Integration**, potvrď.
3. Najdi v HACS "EDC sdílení elektřiny" a klikni **Stáhnout (Download)**.
4. **Restartuj Home Assistant.**

### Varianta B — ruční instalace

1. Zkopíruj složku `custom_components/edc_sdileni/` z tohoto repozitáře do
   `<config>/custom_components/edc_sdileni/` (přes Samba/SSH/Studio Code
   Server doplněk).
2. **Restartuj Home Assistant.**

## Nastavení (přes UI)

1. **Nastavení → Zařízení a služby → Přidat integraci**, vyhledej "EDC
   sdílení elektřiny".
2. Zadej přihlašovací jméno, heslo a první výrobní EAN. Integrace hned
   ověří, že přihlášení funguje a že EAN na portálu existuje — pokud ne,
   uvidíš konkrétní chybu přímo ve formuláři (a detaily v logu).
3. Hotovo — vznikne zařízení pro daný EAN se třemi entitami.

### Přidání dalšího EAN / úprava nastavení

**Nastavení → Zařízení a služby → EDC sdílení elektřiny → Možnosti:**

- **Přidat EAN** — zadáš další výrobní EAN, ověří se stejně jako při
  prvním nastavení a vznikne pro něj nové zařízení.
- **Odebrat EAN** — vybereš ze seznamu nastavených EAN.
- **Nastavení stahování** — hodina/minuta denní aktualizace, "Začátek
  historie" (RRRR-MM-DD) a kolik dní zpět doplňovat, když začátek historie
  není zadaný.

### Změna přihlašovacího jména/hesla

Pokud EDC účet změní heslo (nebo ho integrace sama odmítne kvůli neplatným
údajům), Home Assistant nabídne notifikaci **"Znovu ověřit"** u této
integrace — klikni na ni a zadej nové údaje. Není potřeba integraci mazat
a přidávat znovu.

## Lovelace karta (příklad)

```yaml
type: entities
title: EDC sdílení elektřiny
entities:
  - entity: sensor.edc_859182400312427576_vyroba_export
    name: Výroba (export)
  - entity: sensor.edc_859182400312427576_uspesne_sdileno
    name: Úspěšně sdíleno
  - entity: sensor.edc_859182400312427576_podil_sdilene_elektriny
    name: Podíl sdíleno
```

(Přesné `entity_id` si HA vygeneruje z názvu zařízení — zkontroluj v
**Nastavení → Zařízení a služby → Entity**.)

### Skládaný graf export vs. sdílení

Hotové konfigurace najdeš v **[`examples/lovelace-graf.yaml`](examples/lovelace-graf.yaml)**
— skládaný sloupcový graf po dnech i po měsících, celá sekce dashboardu,
varianta pro víc EANů a jedna verze bez ApexCharts.

Grafy tam nečtou recorder, ale atribut **`historie_dni`** energetických entit.
Ten obsahuje celou dosud známou historii po dnech:

```json
{"2026-08-01": {"measured": 24.1, "shared": 21.2}, ...}
```

Díky tomu se graf vykreslí kompletní hned po instalaci, včetně historie
doplněné zpětně z portálu, a nezáleží na tom, jak dlouho a jak podrobně ti
recorder drží data.

Jedna věc, na kterou se dá naletět: naskládat na sebe *export* a *sdíleno* by
tu samou elektřinu počítalo dvakrát — sdíleno je podmnožina exportu. Skládá se
proto `sdíleno` + `nesdíleno` (= `measured - shared`), takže výška celého
sloupce odpovídá celkovému exportu.

### Detail dne po 15 minutách

Entita výroby má navíc atribut **`detail_15min`** s celou 15minutovou křivkou
posledního uzavřeného dne:

```json
{"datum": "2026-08-02",
 "casy":    ["00:00", "00:15", "..."],
 "vyroba":  [0.01, 0.01, "..."],
 "sdileno": [0.0, 0.0, "..."]}
```

Portál tento detail vrací v téže odpovědi, ze které se počítají denní součty —
integrace si ho jen odloží místo aby ho zahodila, takže to nestojí žádný
požadavek navíc. Časy jsou z odpovědi, ne dopočítané jako `index * 15 min`:
v den změny času má den 92 nebo 100 intervalů a dopočítané časy by se po
přechodu rozešly o hodinu.

Atribut je jen na entitě výroby, protože obsahuje obě série. Zabírá ~2 kB a
mění se jednou denně, takže je to pro recorder zanedbatelné — vyřazovat ho
z databáze není potřeba. Karta je v příkladech jako `6)`.

## Ladění

Pokud senzory zůstávají `unavailable`/`unknown`, zkontroluj log
(**Nastavení → Systém → Protokoly**, filtr `edc_sdileni`) — chybové hlášky
z přihlášení i z API volání se tam propisují včetně části odpovědi serveru
a informace o naplánovaném dalším pokusu (5 min / 1 hod). Upozornění na
neexistující EAN najdeš i v **Nastavení → Opravy (Repairs)**.

## Jak to funguje pod kapotou

Portál `portal.edc-cr.cz` je frontend, který volá:

- `POST https://sso.portal.edc-cr.cz/auth/realms/edc/protocol/openid-connect/token`
  — Keycloak token endpoint (standardní OAuth2/OIDC, grant `password`)
- `POST https://api.portal.edc-cr.cz/api/v0/profiles-data/standard/overview`
  — vrátí 15minutová data pro zadaný EAN a rozsah `dateFrom`/`dateTo`, a
  seznam `missingEans` pro EANy, které portál vůbec nezná

Integrace tohle jen replikuje bez nutnosti spouštět prohlížeč, ukládá denní
součty do vlastního perzistentního úložiště
(`.storage/edc_sdileni_history_<EAN>`) a z něj počítá hodnoty senzorů.

### Co znamenají sloupce `IN` a `OUT`

Odpověď má pro každý EAN dva sloupce, popsané v `valueColumns` přes `dir`.
Jejich význam je ověřený proti portálovému přehledu „Podíl spotřeby energie"
za 31. 7. 2026:

| | hodnota | co to je v portálu |
|---|---|---|
| `IN` | 41,52 kWh | celková dodávka (export) |
| `OUT` | 39,84 kWh | **Prodáno obchodníkovi** |
| `IN − OUT` | 1,68 kWh | **Sdílená energie** (portál uvádí 4,0 %) |

`OUT` tedy **není** sdílený objem, ale ta část dodávky, která ze sdílení odešla
obchodníkovi. Sdílení je až rozdíl. Verze do 1.2 včetně brala `OUT` jako
„sdíleno", takže obě čísla reportovala obráceně; 1.2.1 to opravuje a při prvním
startu **přepočítá i už uloženou historii** (`shared = measured − shared`) —
bez nového dotazu na portál, takže se měsíce zpětně doplněných dat neztratí.

## Poznámka k testování

Kód byl ověřen staticky (syntaxe, importy, logika chunkování a backfillu na
simulovaných datech) a proti stubu Home Assistant API, ale **nebyl zatím
spuštěný v reálné instanci Home Assistanta**. Pokud narazíš na chybu
specifickou pro tvou verzi HA, napiš ji prosím jako Issue na GitHubu.

## Licence

MIT — viz [LICENSE](LICENSE).
