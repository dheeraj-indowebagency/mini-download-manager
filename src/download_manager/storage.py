"""SQLite-based storage manager for download history persistence."""

import sqlite3
import os
from typing import List, Optional

from download_manager.models import DownloadItem, DownloadStatus


class StorageManager:
    def __init__(self, db_path: str = "") -> None:
        if not db_path:
            app_data = os.path.join(os.path.expanduser("~"), ".download_manager")
            os.makedirs(app_data, exist_ok=True)
            db_path = os.path.join(app_data, "downloads.db")
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                file_name TEXT NOT NULL,
                save_path TEXT NOT NULL,
                status TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                downloaded_size INTEGER DEFAULT 0,
                error_message TEXT DEFAULT '',
                created_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )
        self._conn.commit()

    def save_download(self, item: DownloadItem) -> None:
        assert self._conn is not None
        self._conn.execute(
            """
            INSERT OR REPLACE INTO downloads
                (id, url, file_name, save_path, status, file_size,
                 downloaded_size, error_message, created_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.url,
                item.file_name,
                item.save_path,
                item.status.value,
                item.file_size,
                item.downloaded_size,
                item.error_message,
                item.created_at,
                item.completed_at,
            ),
        )
        self._conn.commit()

    def load_downloads(self) -> List[DownloadItem]:
        assert self._conn is not None
        cursor = self._conn.execute(
            """
            SELECT id, url, file_name, save_path, status, file_size,
                   downloaded_size, error_message, created_at, completed_at
            FROM downloads ORDER BY created_at DESC
            """
        )
        items: List[DownloadItem] = []
        for row in cursor.fetchall():
            item = DownloadItem(
                url=row[1],
                file_name=row[2],
                save_path=row[3],
                id=row[0],
                status=DownloadStatus(row[4]),
                file_size=row[5],
                downloaded_size=row[6],
                error_message=row[7],
                created_at=row[8],
                completed_at=row[9],
            )
            if item.file_size > 0:
                item.progress = (item.downloaded_size / item.file_size) * 100
            items.append(item)
        return items

    def delete_download(self, download_id: str) -> None:
        assert self._conn is not None
        self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        self._conn.commit()

    def clear_completed(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            "DELETE FROM downloads WHERE status = ?",
            (DownloadStatus.COMPLETED.value,),
        )
        self._conn.commit()

    def clear_all(self) -> None:
        assert self._conn is not None
        self._conn.execute("DELETE FROM downloads")
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
