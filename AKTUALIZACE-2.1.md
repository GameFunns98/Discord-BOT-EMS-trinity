# Aktualizace 2.1

Tato verze navazuje osobní složku na vybraný kanál žádosti.

- Rozpozná pole `Kanál žádosti` v novém formuláři osobní složky.
- Otevře vybraný kanál `zadost-*` a načte z něj jméno a pozici.
- Podporuje pole `Pozice o kterou si žádáte`.
- Přejmenuje pouze osobní složku, například na `🚑・luis-diaz`.
- Samotný kanál `zadost-*` nikdy nepřejmenuje.
- Cizí nebo neplatný zdrojový kanál odmítne a zobrazí Windows upozornění.

Stávající `.env` z verze 2.0 funguje beze změny. Nová nastavení mají
bezpečné výchozí hodnoty `zadost-` a 100 prohledávaných zpráv.

## Aktualizace nainstalované aplikace

1. Ukončete běžící aplikaci přes pravé tlačítko na tray ikoně a **Ukončit**.
2. V této složce spusťte:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start-bot.ps1
```

Instalátor zachová existující token i ostatní nastavení v `.env`.

Pokud spouštíte aplikaci přes Plánovač úloh, nepoužívejte současně druhé
automatické spuštění. Po instalaci lze zápis automatického spuštění odstranit:

```powershell
powershell -ExecutionPolicy Bypass -File .\remove-autostart.ps1
```
