import asyncio
import signal
import sys

from src.utils.config import config
from src.utils.logger import tampilkan_header, log
from src.api.clob_client import ClobClient
from src.execute.loop import run_hourly_updown_mode


def main():
    tampilkan_header()

    if config.DRY_RUN:
        log.warning("[yellow]Mode DRY RUN aktif — tidak ada order nyata.[/yellow]")

    clob = ClobClient()
    if not clob.hubungkan():
        log.error("[red]Gagal terhubung ke API. Periksa .env[/red]")
        sys.exit(1)

    async def _run():
        stop_event = asyncio.Event()

        def shutdown(sig, frame):
            log.info("[yellow]Shutdown... membatalkan order aktif.[/yellow]")
            clob.batalkan_semua_order()
            stop_event.set()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        task = asyncio.create_task(run_hourly_updown_mode(clob))
        await stop_event.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
