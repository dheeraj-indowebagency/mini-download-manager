"""Download manager: queue scheduling, worker allocation, and state transitions."""

import asyncio
import os
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote, urlparse

from download_manager.models import DownloadItem, DownloadStatus
from download_manager.storage import StorageManager
from download_manager.worker import DownloadWorker

MAX_CONCURRENT = 3
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")


class DownloadManager:
    """Manages download queue, workers, and state transitions."""

    def __init__(
        self,
        storage: StorageManager,
        on_item_updated: Callable[[DownloadItem], None],
        on_item_added: Callable[[DownloadItem], None],
        on_item_removed: Callable[[str], None],
    ) -> None:
        self._storage = storage
        self._on_item_updated = on_item_updated
        self._on_item_added = on_item_added
        self._on_item_removed = on_item_removed

        self._items: Dict[str, DownloadItem] = {}
        self._workers: Dict[str, DownloadWorker] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def items(self) -> List[DownloadItem]:
        return list(self._items.values())

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def load_history(self) -> None:
        for item in self._storage.load_downloads():
            # Reset any downloads that were active when the app last closed
            if item.status in (DownloadStatus.DOWNLOADING, DownloadStatus.QUEUED):
                item.status = DownloadStatus.PAUSED
                item.speed = 0.0
                item.remaining_time = 0.0
            self._items[item.id] = item

    def add_download(
        self, url: str, save_dir: str = "", file_name: str = ""
    ) -> DownloadItem:
        if not save_dir:
            save_dir = DEFAULT_DOWNLOAD_DIR
        os.makedirs(save_dir, exist_ok=True)

        if not file_name:
            file_name = self._extract_filename(url)

        save_path = os.path.join(save_dir, file_name)
        item = DownloadItem(url=url, file_name=file_name, save_path=save_path)
        self._items[item.id] = item
        self._storage.save_download(item)
        self._on_item_added(item)
        self._schedule_queued()
        return item

    def pause_download(self, download_id: str) -> None:
        worker = self._workers.get(download_id)
        if worker and worker.item.status == DownloadStatus.DOWNLOADING:
            worker.pause()
            self._storage.save_download(worker.item)
            self._schedule_queued()

    def resume_download(self, download_id: str) -> None:
        item = self._items.get(download_id)
        if not item:
            return
        if item.status == DownloadStatus.PAUSED:
            worker = self._workers.get(download_id)
            if worker:
                if self._active_count() < MAX_CONCURRENT:
                    worker.resume()
                    self._storage.save_download(item)
                else:
                    item.status = DownloadStatus.QUEUED
                    self._on_item_updated(item)
                    self._storage.save_download(item)
            else:
                item.status = DownloadStatus.QUEUED
                self._on_item_updated(item)
                self._storage.save_download(item)
                self._schedule_queued()

    def cancel_download(self, download_id: str) -> None:
        item = self._items.get(download_id)
        if not item:
            return
        if item.status in (
            DownloadStatus.DOWNLOADING,
            DownloadStatus.PAUSED,
            DownloadStatus.QUEUED,
        ):
            worker = self._workers.pop(download_id, None)
            if worker:
                worker.cancel()
            item.status = DownloadStatus.CANCELLED
            item.speed = 0.0
            item.remaining_time = 0.0
            self._on_item_updated(item)
            self._storage.save_download(item)
            # Clean up partial file
            temp_path = item.save_path + ".part"
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            self._schedule_queued()

    def retry_download(self, download_id: str) -> None:
        item = self._items.get(download_id)
        if not item:
            return
        if item.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            item.reset_for_retry()
            self._on_item_updated(item)
            self._storage.save_download(item)
            self._schedule_queued()

    def remove_download(self, download_id: str) -> None:
        item = self._items.get(download_id)
        if not item:
            return
        # Cancel if active
        worker = self._workers.pop(download_id, None)
        if worker:
            worker.cancel()
        # Clean up partial file
        temp_path = item.save_path + ".part"
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        del self._items[download_id]
        self._storage.delete_download(download_id)
        self._on_item_removed(download_id)
        self._schedule_queued()

    def clear_completed(self) -> None:
        completed_ids = [
            did
            for did, item in self._items.items()
            if item.status == DownloadStatus.COMPLETED
        ]
        for did in completed_ids:
            del self._items[did]
            self._on_item_removed(did)
        self._storage.clear_completed()

    def get_stats(self) -> dict:
        total = len(self._items)
        active = sum(
            1
            for item in self._items.values()
            if item.status == DownloadStatus.DOWNLOADING
        )
        queued = sum(
            1
            for item in self._items.values()
            if item.status == DownloadStatus.QUEUED
        )
        total_speed = sum(
            item.speed
            for item in self._items.values()
            if item.status == DownloadStatus.DOWNLOADING
        )
        return {
            "total": total,
            "active": active,
            "queued": queued,
            "total_speed": total_speed,
        }

    def shutdown(self) -> None:
        for worker in self._workers.values():
            worker.cancel()
        for item in self._items.values():
            self._storage.save_download(item)
        self._storage.close()

    # --- Internal ---

    def _active_count(self) -> int:
        return sum(
            1
            for item in self._items.values()
            if item.status == DownloadStatus.DOWNLOADING
        )

    def _schedule_queued(self) -> None:
        available = MAX_CONCURRENT - self._active_count()
        if available <= 0:
            return
        queued = [
            item
            for item in self._items.values()
            if item.status == DownloadStatus.QUEUED
        ]
        queued.sort(key=lambda x: x.created_at)
        for item in queued[:available]:
            self._start_download(item)

    def _start_download(self, item: DownloadItem) -> None:
        if self._loop is None:
            return
        worker = DownloadWorker(
            item=item,
            on_progress=self._handle_progress,
            on_finished=self._handle_finished,
        )
        self._workers[item.id] = worker
        self._loop.call_soon_threadsafe(worker.start)

    def _handle_progress(self, item: DownloadItem) -> None:
        self._on_item_updated(item)

    def _handle_finished(self, item: DownloadItem) -> None:
        self._workers.pop(item.id, None)
        self._storage.save_download(item)
        self._on_item_updated(item)
        self._schedule_queued()

    @staticmethod
    def _extract_filename(url: str) -> str:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = os.path.basename(path)
        if not name or "." not in name:
            name = "download"
        # Sanitize
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "_")
        return name
