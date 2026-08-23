# Aktualizace 2.2

Tato verze kromě přejmenování vloží do nové osobní složky embed se
základními údaji z vybraného kanálu `zadost-*`:

- jméno a příjmení,
- datum narození,
- telefonní číslo,
- pozice.

Embed se při opakovaném zpracování neduplikuje. Pokud již existuje a vstupní
údaje se změnily, bot upraví svou původní zprávu. Telefon ani datum narození
se nezapisují do `TicketRenamer.log`.

## Nová Discord oprávnění

V kategorii osobních složek musí mít bot nově povoleno:

- **Send Messages**,
- **Embed Links**.

Stávající `.env` z verze 2.1 funguje beze změny.

## Aktualizace nainstalované aplikace

1. Ukončete běžící aplikaci přes tray ikonu a volbu **Ukončit**.
2. V projektové složce spusťte:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start-bot.ps1
```

Instalátor zachová token i ostatní nastavení v `.env`.
