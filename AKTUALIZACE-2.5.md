# Aktualizace na Discord Ticket Renamer 2.5.0

## Co je nové

- bezpečné automatické aktualizace stabilních GitHub Releases na Arch Linuxu,
- user systemd služba a desetiminutový aktualizační timer,
- automatický rollback při neúspěšném startu nové verze,
- ruční formulář pro osobní složku bez platného kanálu `zadost-*`,
- persistentní leadership-only příkaz `/doplnit-zadost`,
- stručná barevná konzole, příkaz `doctor` a bezpečně filtrované logy.

## Důležité

- Tato verze sama nepublikuje Release ani neprovádí živý FiveRoster zápis.
- Stávající `.env` zůstává zachovaný a není součástí žádného release balíčku.
- První přechod z ručně spuštěného venv na systemd vyžaduje jednorázové spuštění
  `linux/install.sh`.
- Windows nadále používá tray aplikaci. Automatické nahrazování vydání je v
  2.5.0 určeno pouze pro Linux/systemd.

Podrobný postup je v `README.md`.
