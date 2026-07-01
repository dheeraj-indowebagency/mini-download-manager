"""PyQt6 main window: toolbar, download table, status bar, and dialogs."""

import os
from typing import Dict, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from download_manager.models import DownloadItem, DownloadStatus


def _format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "Unknown"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024  # type: ignore[assignment]
    return f"{size_bytes:.1f} PB"


def _format_speed(speed: float) -> str:
    if speed <= 0:
        return ""
    return f"{_format_size(int(speed))}/s"


def _format_time(seconds: float) -> str:
    if seconds <= 0:
        return ""
    if seconds > 86400:
        return "> 1 day"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# Columns
COL_FILENAME = 0
COL_STATUS = 1
COL_PROGRESS = 2
COL_SPEED = 3
COL_SIZE = 4
COL_REMAINING = 5
COLUMN_HEADERS = ["File Name", "Status", "Progress", "Speed", "Size", "Remaining Time"]


class _Signals(QObject):
    """Thread-safe bridge: asyncio callbacks emit these signals to update UI."""

    item_updated = pyqtSignal(object)
    item_added = pyqtSignal(object)
    item_removed = pyqtSignal(str)


class AddDownloadDialog(QDialog):
    """Dialog for adding a new download URL."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Download")
        self.setMinimumWidth(500)

        layout = QFormLayout(self)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/file.zip")
        layout.addRow("URL:", self.url_edit)

        dir_layout = QHBoxLayout()
        self.dir_edit = QLineEdit()
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.dir_edit.setText(default_dir)
        dir_layout.addWidget(self.dir_edit)
        from PyQt6.QtWidgets import QPushButton

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_layout.addWidget(browse_btn)
        layout.addRow("Save to:", dir_layout)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("(auto-detect from URL)")
        layout.addRow("File name:", self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self.dir_edit.text()
        )
        if directory:
            self.dir_edit.setText(directory)

    def get_values(self) -> tuple[str, str, str]:
        return (
            self.url_edit.text().strip(),
            self.dir_edit.text().strip(),
            self.name_edit.text().strip(),
        )


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Mini Download Manager")
        self.setMinimumSize(900, 500)
        self.resize(1000, 600)

        # Signals bridge for async -> UI updates
        self.signals = _Signals()
        self.signals.item_updated.connect(self._on_item_updated)
        self.signals.item_added.connect(self._on_item_added)
        self.signals.item_removed.connect(self._on_item_removed)

        self._row_map: Dict[str, int] = {}
        self._manager = None  # set later via set_manager

        self._build_toolbar()
        self._build_table()
        self._build_status_bar()

        # Periodic stats refresh
        self._stats_timer = QTimer()
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(1000)

    def set_manager(self, manager: "DownloadManager") -> None:  # noqa: F821
        self._manager = manager

    # --- Build UI ---

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(toolbar.iconSize())
        self.addToolBar(toolbar)

        self._act_add = QAction("Add Download", self)
        self._act_add.setShortcut("Ctrl+N")
        self._act_add.triggered.connect(self._on_add)
        toolbar.addAction(self._act_add)

        toolbar.addSeparator()

        self._act_pause = QAction("Pause", self)
        self._act_pause.triggered.connect(self._on_pause)
        toolbar.addAction(self._act_pause)

        self._act_resume = QAction("Resume", self)
        self._act_resume.triggered.connect(self._on_resume)
        toolbar.addAction(self._act_resume)

        self._act_cancel = QAction("Cancel", self)
        self._act_cancel.triggered.connect(self._on_cancel)
        toolbar.addAction(self._act_cancel)

        self._act_retry = QAction("Retry", self)
        self._act_retry.triggered.connect(self._on_retry)
        toolbar.addAction(self._act_retry)

        toolbar.addSeparator()

        self._act_remove = QAction("Remove", self)
        self._act_remove.setShortcut("Delete")
        self._act_remove.triggered.connect(self._on_remove)
        toolbar.addAction(self._act_remove)

        self._act_clear = QAction("Clear Completed", self)
        self._act_clear.triggered.connect(self._on_clear_completed)
        toolbar.addAction(self._act_clear)

    def _build_table(self) -> None:
        self._table = QTableWidget(0, len(COLUMN_HEADERS))
        self._table.setHorizontalHeaderLabels(COLUMN_HEADERS)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(COL_FILENAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(
            COL_STATUS, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            COL_PROGRESS, QHeaderView.ResizeMode.Fixed
        )
        self._table.setColumnWidth(COL_PROGRESS, 180)
        header.setSectionResizeMode(
            COL_SPEED, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            COL_SIZE, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(
            COL_REMAINING, QHeaderView.ResizeMode.ResizeToContents
        )

        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        self.setCentralWidget(self._table)

    def _build_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)

        self._lbl_total = QLabel("Total: 0")
        self._lbl_active = QLabel("Active: 0")
        self._lbl_queued = QLabel("Queued: 0")
        self._lbl_speed = QLabel("Speed: 0 B/s")

        for lbl in (self._lbl_total, self._lbl_active, self._lbl_queued, self._lbl_speed):
            lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            status.addPermanentWidget(lbl)

    # --- Populate from history ---

    def populate_from_history(self, items: list[DownloadItem]) -> None:
        for item in items:
            self._add_row(item)

    # --- Toolbar handlers ---

    def _selected_id(self) -> Optional[str]:
        row = self._table.currentRow()
        if row < 0:
            return None
        for did, r in self._row_map.items():
            if r == row:
                return did
        return None

    def _on_add(self) -> None:
        dialog = AddDownloadDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            url, save_dir, file_name = dialog.get_values()
            if not url:
                QMessageBox.warning(self, "Error", "URL cannot be empty.")
                return
            if not url.startswith(("http://", "https://")):
                QMessageBox.warning(
                    self, "Error", "Only HTTP/HTTPS URLs are supported."
                )
                return
            if self._manager:
                self._manager.add_download(url, save_dir, file_name)

    def _on_pause(self) -> None:
        did = self._selected_id()
        if did and self._manager:
            self._manager.pause_download(did)

    def _on_resume(self) -> None:
        did = self._selected_id()
        if did and self._manager:
            self._manager.resume_download(did)

    def _on_cancel(self) -> None:
        did = self._selected_id()
        if did and self._manager:
            self._manager.cancel_download(did)

    def _on_retry(self) -> None:
        did = self._selected_id()
        if did and self._manager:
            self._manager.retry_download(did)

    def _on_remove(self) -> None:
        did = self._selected_id()
        if did and self._manager:
            self._manager.remove_download(did)

    def _on_clear_completed(self) -> None:
        if self._manager:
            self._manager.clear_completed()

    # --- Context menu ---

    def _on_context_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        did = self._selected_id()
        if not did or not self._manager:
            return
        item = self._manager._items.get(did)
        if not item:
            return

        menu = QMenu(self)
        if item.status == DownloadStatus.DOWNLOADING:
            menu.addAction("Pause", lambda: self._manager.pause_download(did))
            menu.addAction("Cancel", lambda: self._manager.cancel_download(did))
        elif item.status == DownloadStatus.PAUSED:
            menu.addAction("Resume", lambda: self._manager.resume_download(did))
            menu.addAction("Cancel", lambda: self._manager.cancel_download(did))
        elif item.status == DownloadStatus.QUEUED:
            menu.addAction("Cancel", lambda: self._manager.cancel_download(did))
        elif item.status in (DownloadStatus.FAILED, DownloadStatus.CANCELLED):
            menu.addAction("Retry", lambda: self._manager.retry_download(did))
        menu.addSeparator()
        menu.addAction("Remove", lambda: self._manager.remove_download(did))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    # --- Signal handlers (UI thread) ---

    def _on_item_added(self, item: DownloadItem) -> None:
        self._add_row(item)
        self._refresh_stats()

    def _on_item_updated(self, item: DownloadItem) -> None:
        row = self._row_map.get(item.id)
        if row is None:
            return
        self._update_row(row, item)
        self._refresh_stats()

    def _on_item_removed(self, download_id: str) -> None:
        row = self._row_map.pop(download_id, None)
        if row is None:
            return
        self._table.removeRow(row)
        # Rebuild row map
        new_map: Dict[str, int] = {}
        for did, r in self._row_map.items():
            if r > row:
                new_map[did] = r - 1
            else:
                new_map[did] = r
        self._row_map = new_map
        self._refresh_stats()

    # --- Table helpers ---

    def _add_row(self, item: DownloadItem) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._row_map[item.id] = row

        self._table.setItem(row, COL_FILENAME, QTableWidgetItem(item.file_name))
        self._table.setItem(row, COL_STATUS, QTableWidgetItem(item.status.value))

        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(int(item.progress))
        progress_bar.setTextVisible(True)
        progress_bar.setFormat(f"{item.progress:.1f}%")
        self._table.setCellWidget(row, COL_PROGRESS, progress_bar)

        self._table.setItem(row, COL_SPEED, QTableWidgetItem(_format_speed(item.speed)))

        if item.file_size > 0:
            size_text = f"{_format_size(item.downloaded_size)} / {_format_size(item.file_size)}"
        else:
            size_text = _format_size(item.downloaded_size) if item.downloaded_size else "Unknown"
        self._table.setItem(row, COL_SIZE, QTableWidgetItem(size_text))

        self._table.setItem(
            row, COL_REMAINING, QTableWidgetItem(_format_time(item.remaining_time))
        )

    def _update_row(self, row: int, item: DownloadItem) -> None:
        self._table.item(row, COL_FILENAME).setText(item.file_name)

        status_item = self._table.item(row, COL_STATUS)
        status_item.setText(item.status.value)

        # Color code status
        color_map = {
            DownloadStatus.QUEUED: "#888888",
            DownloadStatus.DOWNLOADING: "#2196F3",
            DownloadStatus.PAUSED: "#FF9800",
            DownloadStatus.COMPLETED: "#4CAF50",
            DownloadStatus.FAILED: "#F44336",
            DownloadStatus.CANCELLED: "#9E9E9E",
        }
        from PyQt6.QtGui import QColor

        color = color_map.get(item.status, "#000000")
        status_item.setForeground(QColor(color))

        progress_bar = self._table.cellWidget(row, COL_PROGRESS)
        if isinstance(progress_bar, QProgressBar):
            progress_bar.setValue(int(item.progress))
            progress_bar.setFormat(f"{item.progress:.1f}%")

        self._table.item(row, COL_SPEED).setText(_format_speed(item.speed))

        if item.file_size > 0:
            size_text = f"{_format_size(item.downloaded_size)} / {_format_size(item.file_size)}"
        else:
            size_text = (
                _format_size(item.downloaded_size) if item.downloaded_size else "Unknown"
            )
        self._table.item(row, COL_SIZE).setText(size_text)

        self._table.item(row, COL_REMAINING).setText(
            _format_time(item.remaining_time)
        )

    def _refresh_stats(self) -> None:
        if not self._manager:
            return
        stats = self._manager.get_stats()
        self._lbl_total.setText(f"  Total: {stats['total']}  ")
        self._lbl_active.setText(f"  Active: {stats['active']}  ")
        self._lbl_queued.setText(f"  Queued: {stats['queued']}  ")
        self._lbl_speed.setText(f"  Speed: {_format_speed(stats['total_speed'])}  ")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._manager:
            self._manager.shutdown()
        event.accept()
