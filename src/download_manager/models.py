"""Data models for download items."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time
import uuid


class DownloadStatus(Enum):
    QUEUED = "Queued"
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"


@dataclass
class DownloadItem:
    url: str
    file_name: str
    save_path: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: DownloadStatus = DownloadStatus.QUEUED
    file_size: int = 0
    downloaded_size: int = 0
    speed: float = 0.0
    progress: float = 0.0
    remaining_time: float = 0.0
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def reset_for_retry(self) -> None:
        self.status = DownloadStatus.QUEUED
        self.downloaded_size = 0
        self.speed = 0.0
        self.progress = 0.0
        self.remaining_time = 0.0
        self.error_message = ""
        self.completed_at = None
