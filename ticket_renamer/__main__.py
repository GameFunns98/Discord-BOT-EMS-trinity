import sys

from .cli import main


if __name__ == "__main__":
    # Zachovat kompatibilitu se starým `python -m ticket_renamer`, ale nové
    # instalace používají explicitní pod-příkazy.
    raise SystemExit(main(sys.argv[1:] or ["run"]))
