# Discord Ticket Renamer 2.5.0

Samostatný bot pro automatické přejmenování osobních složek zaměstnanců.
Nový tok funguje takto:

1. hráč vyplní žádost v kanálu `zadost-%id%`,
2. při vytvoření osobní složky se vybere **Uživatel** a **Kanál žádosti**,
3. bot otevře vybraný kanál žádosti, načte formulář, přejmenuje osobní
   složku a vloží do ní embed se základními informacemi,
4. podle pozice nabídne vedení cílovou hodnost ve FiveRosteru,
5. po potvrzeném nástupu vloží a připne zaměstnanci herní příkaz
   `/nameinradio` a osobní služební panel.

Pokud odkaz na `zadost-*` chybí nebo není použitelný, bot místo tichého
ukončení vloží do osobní složky ovládání **Doplnit údaje**. Člen vedení v něm
vybere zaměstnance a pozici a doplní jméno, datum narození a telefon. Takto
uložené údaje se následně používají jako autoritativní zdroj a pozdější změna
odkazu na žádost je sama nepřepíše.

Kanál `zadost-*` bot nikdy nepřejmenuje. Z formuláře pro název používá pouze:

- `Jméno a Příjmení`
- `Pozice`

| Pozice | Výsledný název |
| --- | --- |
| Záchranář | `🚑・jackob-white` |
| Doktor | `🩺・jackob-white` |
| Ochranka | `🛡️・jackob-white` |

Podporuje původní pole `Pozice` i nové pole `Pozice o kterou si žádáte`.
Do osobní složky odešle jeden embed s poli:

- `Jméno a příjmení`,
- `Datum narození`,
- `Telefonní číslo`,
- `Pozice`.

Při opakovaném zpracování nevytváří kopie. Existující embed vytvořený
tímto botem najde a podle aktuálních údajů aktualizuje. Datum narození ani
telefonní číslo se nezapisují do lokálního logu; zůstávají ale uložené
v odeslané Discord zprávě v osobní složce.

## FiveRoster nástup

Je-li v `.env` vyplněný API klíč a UUID EMS rosteru, nová osobní složka dostane
samostatnou nástupovou zprávu:

| Pozice v žádosti | Akce |
| --- | --- |
| Záchranář | `Paramedic` nebo `Akademie` |
| Doktor | `Doktor` nebo `Doktor v zácviku` |
| Ochranka | `Security` |

Tlačítka může použít jen člen s rolí vedení `1526254418784424168`. Po
potvrzeném zápisu bot odebere Občana, přidá EMS a nakonfigurované dekorativní
role. Ostatní role zaměstnance zachová. Volbu potom uzamkne, aby nešlo nástup
provést dvakrát.

Po potvrzeném nástupu odešle a připne také kopírovatelný návrh jména a hotový
herní příkaz, například:

```text
F. Lakatoš
/nameinradio A-01 F. Lakatoš
```

Volačku načte z FiveRoster API i u zaměstnance, který už na stejné hodnosti je.
Pokud ji API výjimečně neposkytne, zpráva bezpečně ponechá zástupný text
`[volačka]`. FiveRoster API změnu zobrazovaného jména nepodporuje, proto se
návrh jména nadále nastaví ručně.

Když bot připne svou zprávu, odstraní v osobní složce nově vzniklé systémové
hlášení Discordu o připnutí. Samotná připnutá zpráva ani hlášení po připnutí
cizí zprávy se nemažou.

## Směny, kvóty a LOA

Po dokončeném nebo částečně dokončeném FiveRoster nástupu bot vytvoří právě
jeden připnutý služební panel. Panel přepíná mezi stavy **mimo službu**,
**ve službě** a **LOA** a zobrazuje:

- čas a počet uzavřených směn za tento týden,
- stav všech aktivních FiveRoster kvót,
- začátek a aktuální délku probíhající služby,
- čekající, schválené a právě platné LOA.

Zaměstnanec může pouze na vlastním panelu vstoupit nebo vystoupit ze služby,
odeslat LOA formulář, potvrzenou či čekající LOA zrušit a panel obnovit. Vedení
ani administrátor nemůže směnu ovládat za něj.

Chybějící panel lze ručně doplnit přímo v cílové osobní složce:

```text
/sluzebni-panel uživatel:@zaměstnanec
```

Command smí použít pouze role vedení nastavená v
`ONBOARDING_OPERATOR_ROLE_IDS`. Bot ověří členství ve FiveRosteru, existující
panel neduplikuje a u ručně založeného panelu uvede člena vedení, který příkaz
spustil. Zprávu vždy odesílá bot; Discord nepovoluje vydávat ji za osobní účet.

## Ruční doplnění žádosti

Obnovovací formulář bot založí pouze u rozpoznaného Ticket Tool formuláře v
povolené kategorii osobních složek. Tlačítko může použít výhradně role vedení;
oprávnění Administrátor tuto kontrolu neobchází. Vedení postupně vybere:

1. Discord zaměstnance,
2. pozici `Záchranář`, `Doktor` nebo `Ochranka`,
3. jméno a příjmení, datum narození a telefonní číslo.

Po odeslání bot pokračuje stejným přejmenováním, informačním embedem,
FiveRoster nástupem a služebním panelem jako u běžné žádosti. Stejný obnovovací
panel se nevytvoří dvakrát a po restartu zůstává funkční. Pro již existující
osobní složku jej může vedení založit příkazem:

```text
/doplnit-zadost
```

Příkaz funguje jen přímo v povolené osobní složce, nikdy v `zadost-*`.

Windows verze běží tiše na pozadí jako ikona v oznamovací oblasti u hodin.
Zobrazuje oznámení při přejmenování osobní složky, ztrátě spojení, chybě oprávnění,
neplatném tokenu a dalších důležitých událostech. Podrobný průběh zapisuje do
`TicketRenamer.log`.

> Aplikace se automaticky spouští po přihlášení do Windows. Nejde o systémovou
> službu, protože služby nemohou zobrazovat tray ikonu v uživatelské relaci.
> Při vypnutém počítači bot neběží; pro to je potřeba externí server.

## Arch Linux a automatické aktualizace

Na Arch Linuxu se bot instaluje jako uživatelská systemd služba. Konfigurace se
oddělí od vydaného programu a zůstane v:

```text
~/.config/discord-ticket-renamer/.env
```

První instalace ze staženého stabilního GitHub Release se provede z kořene
projektu:

```bash
chmod +x linux/install.sh
./linux/install.sh
```

Instalátor zachová nalezený `.env`, před případnou migrací jej zazálohuje a
zapne dvě oddělené služby: samotného bota a aktualizátor. Pro běh bez přihlášení
je jednorázově potřeba zapnout lingering pro daného uživatele:

```bash
loginctl enable-linger "$USER"
```

Aktualizátor každých deset minut zkontroluje nejnovější stabilní GitHub Release.
Novou verzi stáhne a otestuje vedle běžící verze. Bota zastaví teprve při
atomickém přepnutí; pokud se nová verze do 60 sekund nepřipojí k Discordu, vrátí
automaticky předchozí funkční vydání. Síťová chyba, poškozený archiv ani chyba
instalace aktuálně běžícího bota nevypne. `.env` není součástí release archivu a
aktualizátor jej nikdy nepřepisuje.

Přehledné ovládání poskytují příkazy:

```bash
ticket-renamer status
ticket-renamer logs
ticket-renamer doctor
ticket-renamer restart
ticket-renamer update-now
ticket-renamer version
```

`logs` sleduje stručný systemd journal. Interaktivní terminál používá barvy,
journal ukládá stejný text bez řídicích znaků. `doctor` také upozorní na staré
`TicketRenamer.log*` v původním adresáři, protože dřívější DEBUG logy mohou
obsahovat citlivé údaje; nikdy je automaticky nemaže.

O dostupné verzi rozhoduje stabilní GitHub Release tag a přiložený
`release-manifest.json`. Verze uvedená v tomto README se při sestavení musí
shodovat s tagem i metadaty balíčku, sama ale aktualizaci neřídí.

## 1. Nastavení bota na Discordu

V [Discord Developer Portal](https://discord.com/developers/applications):

1. Otevřete svou aplikaci a stránku **Bot**.
2. Zapněte **Message Content Intent**.
3. Pozvěte bota na server s oprávněními:
   - **View Channels**,
   - **Read Message History**,
   - **Manage Channels**,
   - **Send Messages**,
   - **Embed Links**,
   - **Pin Messages**,
   - **Manage Messages**,
   - **Manage Roles**.
4. V kategorii osobních složek povolte prvních sedm oprávnění; **Manage Roles**
   se nastavuje na úrovni serverové role bota.
5. V kategorii se žádostmi stačí **View Channels** a **Read Message History**.

Bot nepotřebuje Administrátora. Jeho serverová role ale musí být výše než
Občan, EMS, všech pět dekorativních rolí a nejvyšší role přijímaného člena.

## 2. Příprava ve Windows

Projekt obsahuje samostatný `TicketRenamerTray.exe`, takže nepotřebujete
instalovat Python. V rozbalené složce otevřete PowerShell a spusťte:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
notepad "$env:LOCALAPPDATA\DiscordTicketRenamer\.env"
```

Instalátor aplikaci uloží do stabilní uživatelské složky
`%LOCALAPPDATA%\DiscordTicketRenamer`, aby ji bylo možné bezpečně odstranit z
adresáře Stažené soubory. Do otevřeného `.env` doplňte Discord token,
FiveRoster API klíč a UUID EMS rosteru:

```dotenv
DISCORD_BOT_TOKEN=vas_token
TICKET_TOOL_BOT_IDS=1325579039888511056
TICKET_CATEGORY_IDS=1511618288373858435
REQUEST_CHANNEL_PREFIXES=zadost-
REQUEST_HISTORY_LIMIT=100
FIVEROSTER_API_KEY=vas_api_klic
FIVEROSTER_ROSTER_UUID=uuid_ems_rosteru
```

ID Sekretářky, kategorie, Discord rolí i názvy pěti FiveRoster hodností jsou
předvyplněné. Discord token ani FiveRoster API klíč nikomu neposílejte. Do
`TICKET_CATEGORY_IDS` patří jen kategorie, ve kterých
se nacházejí osobní složky určené k přejmenování. Kategorie se žádostmi se
nepřidává; zdrojový kanál bot bezpečně pozná podle prefixu `zadost-`.

`setup.ps1` zároveň zapne automatické spuštění aplikace po přihlášení do Windows.
Při pozdější aktualizaci zachová stávající `.env` včetně tokenu.

## 3. Spuštění

```powershell
powershell -ExecutionPolicy Bypass -File .\start-bot.ps1
```

Ikona aplikace se objeví u hodin, případně pod šipkou pro skryté ikony. Po
oznámení `Bot je připojen` proveďte jeden zkušební tok:

1. vytvořte a vyplňte kanál `zadost-*`,
2. otevřete osobní složku a ve formuláři vyberte příslušný **Kanál žádosti**,
3. po odeslání se osobní složka přejmenuje například na `🚑・luis-diaz`
   a objeví se v ní základní informace, nástupová tlačítka a návrh jména,
4. člen vedení vybere cílovou hodnost (včetně Ochrany = `Security`),
5. po úspěšném zápisu zkontrolujte připnutý příkaz `/nameinradio` a služební
   panel; pomocné systémové hlášení o připnutí má bot odstranit.

Pokud vybraný kanál neexistuje, nemá prefix `zadost-`, neobsahuje formulář
nebo bot nemá oprávnění, Windows zobrazí upozornění s důvodem. Chybějící
**Send Messages** nebo **Embed Links** zabrání odeslání embedu, ale nemusí
zabránit samotnému přejmenování.

Po úspěšném prvním spuštění můžete rozbalený instalační balíček odstranit;
nainstalovaná kopie zůstane v `%LOCALAPPDATA%\DiscordTicketRenamer` a při příštím
přihlášení se spustí sama.

## Tray menu

Kliknutím pravým tlačítkem na ikonu lze:

- zobrazit stav připojení a poslední událost,
- otestovat Windows oznámení,
- restartovat Discord bota,
- otevřít log nebo složku aplikace,
- zapnout či vypnout automatické spuštění,
- aplikaci úplně ukončit.

Barevný stav ikony:

- zelená – připojeno,
- oranžová – připojování nebo obnova spojení,
- červená – závažná chyba,
- šedá – bot je zastavený.

Automatické spuštění lze odstranit také příkazem:

```powershell
powershell -ExecutionPolicy Bypass -File .\remove-autostart.ps1
```

## Existující osobní složky

Ve výchozím stavu bot reaguje pouze na nové nebo upravené embedy. Pro jednorázové
zpracování existujících osobních složek nastavte v `.env`:

```dotenv
SCAN_EXISTING_TICKETS=true
```

Spusťte bota, počkejte na dokončení kontroly a hodnotu vraťte na `false`, aby se
historie zbytečně nečetla při každém dalším startu.

Tento historický scan úmyslně neprovádí FiveRoster enroll ani hromadně
nevytváří nástupová tlačítka. Kromě názvu a základních informací doplní a
připne služební panel pouze tam, kde najde jednoznačný dokončený nebo částečně
dokončený onboarding marker. Zaměstnance nikdy neodhaduje podle názvu kanálu.

## Bezpečnostní ochrany

- Bot přijímá embedy jen od ID uvedených v `TICKET_TOOL_BOT_IDS`.
- Osobní složky mění jen v kategoriích uvedených v `TICKET_CATEGORY_IDS`.
- Jako zdroj přijme pouze textový kanál ze stejného serveru s prefixem
  uvedeným v `REQUEST_CHANNEL_PREFIXES`.
- Kanály `zadost-*` nikdy nepřejmenovává, ani kdyby byly omylem ve stejné kategorii.
- Neznámá pozice kanál nepřejmenuje a zobrazí upozornění.
- Stejný název neposílá Discordu opakovaně.
- Token zůstává v místním `.env` a nevypisuje se do logu ani embedu.
- Stejně je chráněný FiveRoster API klíč; vydaný ZIP žádný `.env` neobsahuje.
- Discord HTTP a gateway loggery jsou i při ladění omezené minimálně na
  `WARNING`. Autorizační hlavičky, bot tokeny, webhookové tokeny a známé tajné
  hodnoty procházejí redakčním filtrem.
- Embed payloady, datum narození, telefonní číslo a důvod LOA se do lokálních
  logů nezapisují.
- Nástupové tlačítko vyžaduje přesně nakonfigurovanou roli vedení. Administrátor
  bez této role nemá výjimku.
- Ruční `/sluzebni-panel` používá stejnou kontrolu role a funguje jen v povolené
  osobní složce. Směnu a LOA ovládá výhradně vlastník panelu podle Discord ID.
- Automatické či ruční připnutí botovy zprávy může vytvořit systémové hlášení
  typu `pins_add`. Bot odstraňuje pouze nové hlášení odkazující na jeho vlastní
  zprávu v povolené osobní složce; k tomu potřebuje **Manage Messages**.
- FiveRoster stav se před zápisem znovu ověřuje. Krátká cache a interní omezení
  drží aplikaci pod limitem 50 požadavků API za minutu.
- Před API zápisem bot ověří existenci a pořadí Discord rolí. Pokud je už člen
  ve FiveRosteru na jiné hodnosti, automaticky ho nepovýší ani nedegraduje.
  Telefonní číslo a datum narození se ze žádosti zkopírují do povolené osobní
  složky, ale nevypisují se do lokálního logu.
- Embed obsahující osobní údaje může číst každý, kdo má přístup k osobní
  složce; oprávnění této kategorie proto udržujte omezená.
- Pro kontrolu změn log používá typ provedené operace bez jména zaměstnance.
  Discord ID, interaction tokeny a další stabilní identifikátory redakční filtr
  nahradí zástupnou hodnotou. Windows log se automaticky rotuje (nejvýše
  4 soubory), Linux používá systemd journal.
- Běžet může jen jedna instance tray aplikace.

## Vývojářské testy

Tato část je potřeba jen při úpravě zdrojového kódu a vyžaduje Python:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Kontrola instalace bez připojení živého bota:

```text
ticket-renamer self-test
ticket-renamer doctor
```
