# Aktualizace 2.3 – FiveRoster nástup

Verze 2.3 přidává do nových osobních složek řízený nástup zaměstnance:

- Záchranář: tlačítka `Paramedic` a `Akademie`.
- Doktor: tlačítka `Doktor` a `Doktor v zácviku`.
- Ochranka: automatický zápis na `Security`; při chybě se zobrazí `Opakovat Security`.
- Tlačítka smí ovládat pouze role vedení `1526254418784424168`.
- Po úspěšném zápisu se odebere Občan, přidá EMS a pět dekorativních rolí.
- Bot pod ovládáním navrhne kopírovatelné jméno, například `F. Lakatoš`.

## Aktualizace

1. Přes tray menu úplně ukončete běžící aplikaci.
2. Spusťte z nového balíčku:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
notepad "$env:LOCALAPPDATA\DiscordTicketRenamer\.env"
```

3. Do místního `.env` vyplňte `FIVEROSTER_API_KEY` a
   `FIVEROSTER_ROSTER_UUID`. Klíč nikomu neposílejte.
4. Ověřte přesné názvy pěti hodností a spusťte:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-bot.ps1
```

Po startu musí přijít oznámení, že FiveRoster a všech pět hodností byly
ověřeny. Pokud některý název neodpovídá, bot neprovede žádný enroll a vypíše
konkrétní chybu.

Bot nově potřebuje oprávnění **Manage Roles** a jeho role musí být nad rolí
Občan, EMS, dekorativními rolemi i nad rolí přijímaného zaměstnance.

Historický scan staré osobní složky automaticky do FiveRosteru nezapisuje.
