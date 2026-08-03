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
| Výroba (export) | naměřená výroba za poslední uzavřený den | kWh |
| Úspěšně sdíleno | kolik z výroby bylo úspěšně sdíleno | kWh |
| Podíl sdílené elektřiny | sdíleno / výroba | % |

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
2. **Přihlašování běží přes tzv. "password grant"** (OAuth2 Resource Owner
   Password Credentials) přímo proti EDC Keycloak SSO. Spousta Keycloak
   instalací tenhle typ přihlášení z bezpečnostních důvodů **vypíná**. Než
   cokoliv nastavíš v Home Assistantu, **ověř si to sám** příkazem níže —
   heslo posíláš jen ze svého vlastního počítače/terminálu, nikam jinam.
3. Heslo bude uložené v konfiguraci Home Assistanta (`.storage/core.config_entries`)
   — chraň zálohy HA stejně jako jakékoliv jiné citlivé údaje.

### Ověřovací test (spusť sám, v terminálu — ne přes AI asistenta)

```bash
curl -s -X POST "https://sso.portal.edc-cr.cz/auth/realms/edc/protocol/openid-connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=a63c22a3-6e1d-4eac-b383-d06373da046a" \
  -d "username=TVOJE_PRIHLASOVACI_JMENO" \
  -d "password=TVOJE_HESLO"
```

- JSON s `"access_token": "..."` → funguje, můžeš pokračovat.
- Chyba `invalid_grant` / `unauthorized_client` / `unsupported_grant_type` →
  EDC tuhle cestu přihlášení nepovoluje a integrace bez úprav nepůjde použít.

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
**Nastavení → Zařízení a služby → Entity**. Hodí se i `gauge` karta na podíl
sdílené elektřiny, nebo `history-graph`/ApexCharts nad všemi entitami napříč
zařízeními.)

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
  — vrátí 15minutová data (`IN` = naměřená výroba, `OUT` = výsledek
  vyhodnocení sdílení) pro zadaný EAN a rozsah `dateFrom`/`dateTo`, a
  seznam `missingEans` pro EANy, které portál vůbec nezná

Integrace tohle jen replikuje bez nutnosti spouštět prohlížeč, ukládá denní
součty do vlastního perzistentního úložiště
(`.storage/edc_sdileni_history_<EAN>`) a z něj počítá hodnoty senzorů.

## Poznámka k testování

Kód byl ověřen staticky (syntaxe, importy, logika chunkování a backfillu na
simulovaných datech) a proti stubu Home Assistant API, ale **nebyl zatím
spuštěný v reálné instanci Home Assistanta**. Pokud narazíš na chybu
specifickou pro tvou verzi HA, napiš ji prosím jako Issue na GitHubu.

## Licence

MIT — viz [LICENSE](LICENSE).
