"""Entry point: wires PyQt6 UI with asyncio event loop in a background thread."""

import asyncio
import sys
import threading

from PyQt6.QtWidgets import QApplication

from download_manager.manager import DownloadManager
from download_manager.models import DownloadItem
from download_manager.storage import StorageManager
from download_manager.ui import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Mini Download Manager")

    window = MainWindow()

    # Start asyncio loop in a daemon thread
    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    # Thread-safe callbacks that emit Qt signals
    def on_item_updated(item: DownloadItem) -> None:
        window.signals.item_updated.emit(item)

    def on_item_added(item: DownloadItem) -> None:
        window.signals.item_added.emit(item)

    def on_item_removed(download_id: str) -> None:
        window.signals.item_removed.emit(download_id)

    storage = StorageManager()
    manager = DownloadManager(
        storage=storage,
        on_item_updated=on_item_updated,
        on_item_added=on_item_added,
        on_item_removed=on_item_removed,
    )
    manager.set_loop(loop)
    manager.load_history()

    window.set_manager(manager)
    window.populate_from_history(manager.items)
    window.show()

    exit_code = app.exec()

    # Cleanup
    manager.shutdown()
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
