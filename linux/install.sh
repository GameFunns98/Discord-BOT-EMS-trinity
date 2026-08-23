#!/usr/bin/env bash
set -Eeuo pipefail

APP="discord-ticket-renamer"
REPOSITORY="GameFunns98/Discord-BOT-EMS-trinity"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENABLE_LINGER=1

usage() {
    cat <<'EOF'
Pouziti: ./linux/install.sh [--source CESTA] [--no-linger]

Nainstaluje bota jako systemd user sluzbu. Existujici .env se zachova,
zdrojovy adresar se nesmaze a aktualizace pozdeji meni pouze odkaz current.
EOF
}

while (($#)); do
    case "$1" in
        --source)
            [[ $# -ge 2 ]] || { echo "Za --source chybi cesta." >&2; exit 2; }
            SOURCE_DIR="$(cd -- "$2" && pwd -P)"
            shift 2
            ;;
        --no-linger)
            ENABLE_LINGER=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Neznamy parametr: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "Tento instalator je urcen pouze pro Linux." >&2; exit 1; }
command -v "$PYTHON_BIN" >/dev/null || { echo "Nainstalujte Python 3.11 nebo novejsi." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "Systemd nebyl nalezen." >&2; exit 1; }
command -v tar >/dev/null || { echo "Program tar nebyl nalezen." >&2; exit 1; }
[[ -f "$SOURCE_DIR/pyproject.toml" && -f "$SOURCE_DIR/linux/updater.py" ]] || {
    echo "Zdrojovy adresar neni kompletni Discord Ticket Renamer projekt." >&2
    exit 1
}

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "Je vyzadovan Python 3.11 nebo novejsi." >&2
    exit 1
}

CONFIG_HOME="$HOME/.config"
DATA_HOME="$HOME/.local/share"
CONFIG_DIR="$CONFIG_HOME/$APP"
DATA_DIR="$DATA_HOME/$APP"
RELEASES_DIR="$DATA_DIR/releases"
LIB_DIR="$HOME/.local/lib/$APP"
BIN_DIR="$HOME/.local/bin"
UNIT_DIR="$CONFIG_HOME/systemd/user"
CONFIG_FILE="$CONFIG_DIR/.env"
LEGACY_SOURCE_FILE="$CONFIG_DIR/legacy-source"

VERSION="$($PYTHON_BIN -c 'import pathlib,re,sys; text=(pathlib.Path(sys.argv[1])/"ticket_renamer"/"__init__.py").read_text(encoding="utf-8"); match=re.search(r"^__version__\s*=\s*[\"\x27](\d+\.\d+\.\d+)[\"\x27]", text, re.M); print(match.group(1) if match else "")' "$SOURCE_DIR" 2>/dev/null)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "Ze zdrojovych souboru nelze zjistit verzi." >&2
    exit 1
}
RELEASE_DIR="$RELEASES_DIR/$VERSION"

mkdir -p "$CONFIG_DIR" "$RELEASES_DIR" "$LIB_DIR" "$BIN_DIR" "$UNIT_DIR"
chmod 700 "$CONFIG_DIR" "$DATA_DIR" "$RELEASES_DIR" "$LIB_DIR"

# Doctor později pouze upozorní na staré potenciálně citlivé DEBUG logy.
# Původní zdroj ani logy se nemažou a při dalších aktualizacích se tato cesta
# nepřepisuje cestou k novému release archivu.
if [[ ! -e "$LEGACY_SOURCE_FILE" && "$SOURCE_DIR" != *$'\n'* ]]; then
    printf '%s\n' "$SOURCE_DIR" >"$CONFIG_DIR/.legacy-source.new"
    chmod 600 "$CONFIG_DIR/.legacy-source.new"
    mv -f "$CONFIG_DIR/.legacy-source.new" "$LEGACY_SOURCE_FILE"
fi

if [[ ! -e "$CONFIG_FILE" ]]; then
    if [[ -f "$SOURCE_DIR/.env" ]]; then
        install -m 600 "$SOURCE_DIR/.env" "$CONFIG_FILE"
        echo "Existujici .env byl prenesen do $CONFIG_FILE."
    elif [[ -f "$SOURCE_DIR/.env.example" ]]; then
        install -m 600 "$SOURCE_DIR/.env.example" "$CONFIG_FILE"
        echo "Byla vytvorena konfigurace z .env.example; doplnte tajne hodnoty."
    else
        printf 'LOG_LEVEL=INFO\n' >"$CONFIG_FILE"
        chmod 600 "$CONFIG_FILE"
        echo "Byla vytvorena prazdna konfigurace; pred spustenim doplnte Discord a FiveRoster hodnoty."
    fi
else
    chmod 600 "$CONFIG_FILE"
    echo "Existujici konfigurace $CONFIG_FILE byla zachovana."
fi

install -m 755 "$SOURCE_DIR/linux/updater.py" "$LIB_DIR/updater.py"
"$PYTHON_BIN" "$LIB_DIR/updater.py" migrate-env "$CONFIG_FILE"

if [[ ! -d "$RELEASE_DIR" ]]; then
    mkdir -m 700 "$RELEASE_DIR"
    tar \
        --exclude='./.git' \
        --exclude='./.env' \
        --exclude='./.venv' \
        --exclude='./.venv-*' \
        --exclude='./build' \
        --exclude='./dist' \
        --exclude='./TicketRenamerTray.exe' \
        --exclude='./TicketRenamer.log*' \
        --exclude='./__pycache__' \
        --exclude='./.pytest_cache' \
        -C "$SOURCE_DIR" -cf - . | tar -C "$RELEASE_DIR" -xf -
fi

if [[ -e "$RELEASE_DIR/.env" && ! -L "$RELEASE_DIR/.env" ]]; then
    echo "Release neocekavane obsahuje vlastni .env; instalace byla zastavena." >&2
    exit 1
fi
ln -sfn "$CONFIG_FILE" "$RELEASE_DIR/.env"

if [[ -f "$RELEASE_DIR/requirements-linux.lock" ]]; then
    REQUIREMENTS="$RELEASE_DIR/requirements-linux.lock"
else
    REQUIREMENTS="$RELEASE_DIR/requirements.txt"
fi

if [[ ! -x "$RELEASE_DIR/.venv/bin/python" ]]; then
    "$PYTHON_BIN" -m venv "$RELEASE_DIR/.venv"
fi
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$REQUIREMENTS"

$PYTHON_BIN - "$RELEASE_DIR/release-manifest.json" "$VERSION" "$REPOSITORY" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "version": sys.argv[2],
    "tag_name": f"v{sys.argv[2]}",
    "repository": sys.argv[3],
    "minimum_python": ">=3.11",
    "installation": "local-source",
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

"$RELEASE_DIR/.venv/bin/python" -m ticket_renamer self-test

install -m 755 "$SOURCE_DIR/linux/ticket-renamer" "$BIN_DIR/ticket-renamer"
install -m 644 "$SOURCE_DIR/linux/systemd/discord-ticket-renamer.service" "$UNIT_DIR/discord-ticket-renamer.service"
install -m 644 "$SOURCE_DIR/linux/systemd/discord-ticket-renamer-update.service" "$UNIT_DIR/discord-ticket-renamer-update.service"
install -m 644 "$SOURCE_DIR/linux/systemd/discord-ticket-renamer-update.timer" "$UNIT_DIR/discord-ticket-renamer-update.timer"

systemctl --user daemon-reload

if ! "$RELEASE_DIR/.venv/bin/python" - "$CONFIG_FILE" <<'PY'
from dotenv import dotenv_values
import pathlib
import sys

value = dotenv_values(pathlib.Path(sys.argv[1])).get("DISCORD_BOT_TOKEN")
raise SystemExit(0 if isinstance(value, str) and value.strip() else 1)
PY
then
    systemctl --user disable --now \
        discord-ticket-renamer.service \
        discord-ticket-renamer-update.timer 2>/dev/null || true
    echo "Instalace je pripravena, ale DISCORD_BOT_TOKEN je prazdny." >&2
    echo "Doplnte $CONFIG_FILE a spustte instalator znovu; sluzba nebyla spustena." >&2
    exit 2
fi

if pgrep -u "$(id -u)" -f '[p]ython[^ ]* .*ticket_renamer|[p]ython[^ ]* .*launcher\.py' >/dev/null \
    && ! systemctl --user is-active --quiet discord-ticket-renamer.service; then
    echo "Byl nalezen rucne spusteny bot. Ukoncete jej a instalator spustte znovu." >&2
    exit 1
fi

if systemctl --user is-active --quiet discord-ticket-renamer-update.service; then
    echo "Aktualizator prave pracuje. Pockejte na jeho dokonceni a instalator spustte znovu." >&2
    exit 1
fi

OLD_TARGET=""
if [[ -L "$DATA_DIR/current" ]]; then
    OLD_TARGET="$(readlink -f "$DATA_DIR/current")"
fi
systemctl --user enable discord-ticket-renamer.service discord-ticket-renamer-update.timer
HEALTH_FILE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/discord-ticket-renamer/health.json"

wait_for_health() {
    local expected_version="$1"
    local attempts="${2:-60}"
    local unused
    for unused in $(seq 1 "$attempts"); do
        if systemctl --user is-active --quiet discord-ticket-renamer.service \
            && "$PYTHON_BIN" - "$HEALTH_FILE" "$expected_version" <<'PY' 2>/dev/null
import datetime
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
raw = payload.get("timestamp", "")
stamp = datetime.datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
valid = (
    payload.get("version") == sys.argv[2]
    and payload.get("state") == "ready"
    and payload.get("ready") is True
    and isinstance(payload.get("pid"), int)
    and 0 <= age <= 65
)
raise SystemExit(0 if valid else 1)
PY
        then
            return 0
        fi
        sleep 1
    done
    return 1
}

release_version() {
    "$PYTHON_BIN" - "$1/release-manifest.json" <<'PY' 2>/dev/null
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
version = payload.get("version")
if not isinstance(version, str) or not version.strip():
    raise SystemExit(1)
print(version)
PY
}

restore_previous() {
    systemctl --user stop discord-ticket-renamer.service 2>/dev/null || true
    rm -f "$HEALTH_FILE"
    if [[ -n "$OLD_TARGET" ]]; then
        ln -sfn "$OLD_TARGET" "$DATA_DIR/.current.new" || return 1
        mv -Tf "$DATA_DIR/.current.new" "$DATA_DIR/current" || return 1
        systemctl --user start discord-ticket-renamer.service || return 1
        local old_version
        old_version="$(release_version "$OLD_TARGET")" || return 1
        wait_for_health "$old_version" 60 || return 1
        systemctl --user start discord-ticket-renamer-update.timer || true
        return 0
    fi
    if [[ -L "$DATA_DIR/current" ]]; then
        rm -f "$DATA_DIR/current"
    fi
    systemctl --user disable discord-ticket-renamer.service discord-ticket-renamer-update.timer \
        2>/dev/null || true
    return 0
}

systemctl --user stop discord-ticket-renamer-update.timer 2>/dev/null || true
if ! systemctl --user stop discord-ticket-renamer.service 2>/dev/null; then
    systemctl --user start discord-ticket-renamer-update.timer 2>/dev/null || true
    echo "Puvodni sluzbu nelze zastavit; aktivni verze zustala beze zmeny." >&2
    exit 1
fi

SWITCH_OK=1
if [[ -n "$OLD_TARGET" && "$OLD_TARGET" != "$RELEASE_DIR" ]]; then
    ln -sfn "$OLD_TARGET" "$DATA_DIR/.previous.new" || SWITCH_OK=0
    if ((SWITCH_OK)); then
        mv -Tf "$DATA_DIR/.previous.new" "$DATA_DIR/previous" || SWITCH_OK=0
    fi
fi
if ((SWITCH_OK)); then
    ln -sfn "$RELEASE_DIR" "$DATA_DIR/.current.new" || SWITCH_OK=0
fi
if ((SWITCH_OK)); then
    mv -Tf "$DATA_DIR/.current.new" "$DATA_DIR/current" || SWITCH_OK=0
fi

if ((SWITCH_OK == 0)); then
    echo "Aktivni odkaz nelze prepnout; obnovuji puvodni sluzbu." >&2
    if ! restore_previous; then
        echo "Puvodni sluzbu se nepodarilo potvrdit; zkontrolujte journal." >&2
    fi
    exit 1
fi

rm -f "$HEALTH_FILE"
if ! systemctl --user start discord-ticket-renamer.service; then
    echo "Novou sluzbu nelze spustit; obnovuji puvodni verzi." >&2
    if ! restore_previous; then
        echo "Puvodni sluzbu se nepodarilo potvrdit; zkontrolujte journal." >&2
    fi
    exit 1
fi

if ! wait_for_health "$VERSION" 60; then
    echo "Nova sluzba nepotvrdila Discord ready stav; obnovuji predchozi stav." >&2
    if ! restore_previous; then
        echo "Puvodni sluzbu se nepodarilo potvrdit; zkontrolujte journal." >&2
    fi
    exit 1
fi

if ! systemctl --user start discord-ticket-renamer-update.timer; then
    echo "Bot bezi, ale aktualizacni timer se nepodarilo spustit." >&2
    exit 1
fi

if ((ENABLE_LINGER)); then
    if ! loginctl enable-linger "$USER"; then
        echo "Linger se nepodarilo povolit. Pozdeji spustte: loginctl enable-linger $USER" >&2
    fi
fi

cat <<EOF

Discord Ticket Renamer $VERSION je nainstalovan.
Stav:       ticket-renamer status
Log:        ticket-renamer logs
Kontrola:   ticket-renamer doctor
Aktualizace: ticket-renamer update-now

Pokud prikaz neni nalezen, pridejte $BIN_DIR do PATH.
EOF
