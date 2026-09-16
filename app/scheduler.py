"""Runs create_backup() automatically on a timer for the life of the process."""
import asyncio

from app.backup import create_backup
from app.config import BACKUP_ENABLED, BACKUP_INTERVAL_HOURS, BACKUP_STARTUP_DELAY_SECONDS


async def backup_loop():
    if not BACKUP_ENABLED:
        print("[backup] automatic backups are disabled (BACKUP_ENABLED=false)")
        return

    print(f"[backup] automatic backups enabled - every {BACKUP_INTERVAL_HOURS}h, "
          f"first run in {BACKUP_STARTUP_DELAY_SECONDS}s")
    await asyncio.sleep(BACKUP_STARTUP_DELAY_SECONDS)

    while True:
        try:
            meta = await asyncio.to_thread(create_backup, "auto", "system (scheduled)")
            print(f"[backup] scheduled backup created: {meta['filename']}")
        except Exception as e:  # never let a bad backup kill the app
            print(f"[backup] scheduled backup FAILED: {e}")
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
