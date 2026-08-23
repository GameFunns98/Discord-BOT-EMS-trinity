import sys

from ticket_renamer.bot import run
from ticket_renamer.config import Settings


def main() -> None:
    if "--check-config" in sys.argv[1:]:
        settings = Settings.from_environment()
        print("Konfigurace je v poradku.")
        print(f"Povoleni odesilatele embedu: {len(settings.ticket_tool_bot_ids)}")
        print(f"Povoleni ticketovych kategorii: {len(settings.ticket_category_ids)}")
        return

    run()


if __name__ == "__main__":
    main()
