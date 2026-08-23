# Aktualizace 2.4 – služební panel, směny a LOA

Verze 2.4 přidává po potvrzeném FiveRoster nástupu připnutý osobní panel:

- vstup a ukončení služby,
- týdenní čas, počet směn a stav kvót,
- žádost o LOA a potvrzované zrušení,
- trvalá tlačítka fungující i po restartu aplikace,
- ruční doplnění pomocí `/sluzebni-panel uživatel:@zaměstnanec`.

Panel může ovládat pouze zaměstnanec, kterému patří. Ruční command smí použít
jen role vedení `1526254418784424168`; administrátor bez této role nemá výjimku.

## Aktualizace

1. Přes tray menu úplně ukončete běžící aplikaci.
2. V roli bota na Discordu nově povolte **Pin Messages**. V kategorii osobních
   složek ponechte také View Channels, Read Message History, Send Messages a
   Embed Links.
3. Spusťte z nového balíčku:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
powershell -ExecutionPolicy Bypass -File .\start-bot.ps1
```

Instalátor zachová existující `.env`, Discord token i FiveRoster API klíč.
Slash command se po připojení synchronizuje jen na server, kde bot najde
nakonfigurovanou kategorii osobních složek.

## Bezpečný první test

Ostrou směnu nebo LOA nejprve nezkoušejte na běžném zaměstnanci. V určené
testovací osobní složce zkontrolujte, že se po nástupu objeví jeden připnutý
panel a že `/sluzebni-panel` stejnou zprávu pouze obnoví. Teprve potom použijte
start/end služby na potvrzeném testovacím účtu.
