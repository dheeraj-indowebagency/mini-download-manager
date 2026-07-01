"""Async download worker: downloads file chunks with pause/resume support."""

import asyncio
import os
import time
from typing import Callable, Optional

import aiohttp

from download_manager.models import DownloadItem, DownloadStatus

CHUNK_SIZE = 64 * 1024  # 64 KB


class DownloadWorker:
    """Handles the actual downloading of a single file."""

    def __init__(
        self,
        item: DownloadItem,
        on_progress: Callable[[DownloadItem], None],
        on_finished: Callable[[DownloadItem], None],
    ) -> None:
        self._item = item
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially
        self._cancelled = False
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def item(self) -> DownloadItem:
        return self._item

    def start(self) -> None:
        self._task = asyncio.ensure_future(self._download())

    def pause(self) -> None:
        self._pause_event.clear()
        self._item.status = DownloadStatus.PAUSED
        self._item.speed = 0.0
        self._item.remaining_time = 0.0
        self._on_progress(self._item)

    def resume(self) -> None:
        self._item.status = DownloadStatus.DOWNLOADING
        self._pause_event.set()
        self._on_progress(self._item)

    def cancel(self) -> None:
        self._cancelled = True
        self._pause_event.set()  # unblock if paused
        self._item.status = DownloadStatus.CANCELLED
        self._item.speed = 0.0
        self._item.remaining_time = 0.0
        if self._task and not self._task.done():
            self._task.cancel()

    async def _download(self) -> None:
        item = self._item
        item.status = DownloadStatus.DOWNLOADING
        self._on_progress(item)

        temp_path = item.save_path + ".part"
        file_mode = "ab" if os.path.exists(temp_path) else "wb"
        if file_mode == "ab":
            item.downloaded_size = os.path.getsize(temp_path)

        headers: dict[str, str] = {}
        if item.downloaded_size > 0:
            headers["Range"] = f"bytes={item.downloaded_size}-"

        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(item.url, headers=headers) as response:
                    if response.status not in (200, 206):
                        item.status = DownloadStatus.FAILED
                        item.error_message = (
                            f"HTTP {response.status}: {response.reason}"
                        )
                        self._on_finished(item)
                        return

                    if item.file_size == 0:
                        content_length = response.headers.get("Content-Length")
                        if content_length:
                            item.file_size = int(content_length)

                    speed_window_start = time.monotonic()
                    speed_window_bytes = 0
                    last_update = time.monotonic()

                    with open(temp_path, file_mode) as f:
                        async for chunk in response.content.iter_chunked(
                            CHUNK_SIZE
                        ):
                            if self._cancelled:
                                self._on_finished(item)
                                return

                            await self._pause_event.wait()

                            if self._cancelled:
                                self._on_finished(item)
                                return

                            f.write(chunk)
                            item.downloaded_size += len(chunk)
                            speed_window_bytes += len(chunk)

                            now = time.monotonic()
                            elapsed = now - speed_window_start
                            if elapsed >= 0.5:
                                item.speed = speed_window_bytes / elapsed
                                speed_window_start = now
                                speed_window_bytes = 0

                                if (
                                    item.file_size > 0
                                    and item.speed > 0
                                ):
                                    remaining = (
                                        item.file_size - item.downloaded_size
                                    )
                                    item.remaining_time = remaining / item.speed

                            if item.file_size > 0:
                                item.progress = (
                                    item.downloaded_size / item.file_size
                                ) * 100

                            if now - last_update >= 0.25:
                                self._on_progress(item)
                                last_update = now

            if self._cancelled:
                self._on_finished(item)
                return

            # Rename temp file to final path
            if os.path.exists(item.save_path):
                base, ext = os.path.splitext(item.save_path)
                counter = 1
                while os.path.exists(item.save_path):
                    item.save_path = f"{base} ({counter}){ext}"
                    counter += 1

            os.rename(temp_path, item.save_path)

            item.status = DownloadStatus.COMPLETED
            item.progress = 100.0
            item.speed = 0.0
            item.remaining_time = 0.0
            item.completed_at = time.time()
            self._on_finished(item)

        except asyncio.CancelledError:
            item.speed = 0.0
            item.remaining_time = 0.0
            self._on_finished(item)
        except aiohttp.ClientError as e:
            item.status = DownloadStatus.FAILED
            item.error_message = str(e)
            item.speed = 0.0
            item.remaining_time = 0.0
            self._on_finished(item)
        except OSError as e:
            item.status = DownloadStatus.FAILED
            item.error_message = f"File error: {e}"
            item.speed = 0.0
            item.remaining_time = 0.0
            self._on_finished(item)
