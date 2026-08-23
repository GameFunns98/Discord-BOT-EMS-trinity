from __future__ import annotations

import asyncio
import logging
from threading import Lock, Thread, current_thread

import discord

from .bot import TicketRenamerClient
from .config import Settings
from .events import AppEvent, EventLevel, EventReporter


LOGGER = logging.getLogger("ticket_renamer.controller")


class BotController:
    def __init__(self, settings: Settings, reporter: EventReporter) -> None:
        self.settings = settings
        self.reporter = reporter
        self._lock = Lock()
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: TicketRenamerClient | None = None
        self._stop_requested = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_requested = False
            self._thread = Thread(
                target=self._thread_main,
                name="DiscordBot",
                daemon=True,
            )
            self._thread.start()

        self.reporter(
            AppEvent(
                EventLevel.INFO,
                "Ticket Renamer",
                "Bot se připojuje k Discordu.",
                status="Připojování…",
                notify=False,
            )
        )

    def restart(self) -> None:
        self.stop()
        self.start()

    def stop(self, timeout: float = 12.0) -> None:
        with self._lock:
            self._stop_requested = True
            loop = self._loop
            client = self._client
            thread = self._thread

        if loop is not None and client is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(client.close(), loop)
                future.result(timeout=timeout)
            except Exception:
                LOGGER.exception("Nepodařilo se korektně ukončit Discord klienta.")

        if thread is not None and thread is not current_thread():
            thread.join(timeout=timeout)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            LOGGER.exception("Vlákno Discord bota neočekávaně skončilo.")
            self.reporter(
                AppEvent(
                    EventLevel.ERROR,
                    "Bot se zastavil",
                    "Discord bot neočekávaně skončil. Podrobnosti jsou v logu.",
                    status="Chyba",
                )
            )

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        client = TicketRenamerClient(self.settings, reporter=self.reporter)

        with self._lock:
            self._loop = loop
            self._client = client

        error_reported = False
        try:
            await client.start(self.settings.discord_bot_token, reconnect=True)
        except discord.LoginFailure:
            error_reported = True
            LOGGER.exception("Discord odmítl token bota.")
            self.reporter(
                AppEvent(
                    EventLevel.ERROR,
                    "Neplatný token",
                    "Discord odmítl token. Opravte DISCORD_BOT_TOKEN v souboru .env.",
                    status="Chyba přihlášení",
                )
            )
        except discord.PrivilegedIntentsRequired:
            error_reported = True
            LOGGER.exception("Není povolen Message Content Intent.")
            self.reporter(
                AppEvent(
                    EventLevel.ERROR,
                    "Chybí Discord oprávnění",
                    "V Developer Portalu zapněte Message Content Intent.",
                    status="Chyba oprávnění",
                )
            )
        except Exception:
            error_reported = True
            LOGGER.exception("Discord klient skončil chybou.")
            self.reporter(
                AppEvent(
                    EventLevel.ERROR,
                    "Chyba připojení",
                    "Bot se nemohl připojit. Podrobnosti jsou v logu.",
                    status="Chyba",
                )
            )
        finally:
            with self._lock:
                stopped_by_user = self._stop_requested
                self._client = None
                self._loop = None

            if not stopped_by_user and not error_reported:
                self.reporter(
                    AppEvent(
                        EventLevel.WARNING,
                        "Bot není spuštěný",
                        "Připojení skončilo. Použijte v tray menu Restartovat bota.",
                        status="Zastaveno",
                    )
                )

