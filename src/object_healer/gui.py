"""ObjectHealer drag-and-drop window: healed copies are written next to the originals."""
import os
import sys
from importlib import resources

from PyQt6.QtCore import QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QCheckBox,
    QLabel, QPushButton, QScrollArea, QFrame, QSizePolicy, QFileDialog,
)

from . import floaters
from .blender import SUPPORTED, BlenderNotFound, find_blender, heal_file

STYLE = """
QWidget { background: #1e1e2e; color: #cdd6f4;  font-size: 13px; }
QPushButton { border: none; border-radius: 6px; padding: 6px 16px; }
QPushButton#heal { background: #7c6af7; color: white; font-weight: bold; font-size: 13px; }
QPushButton#heal:hover { background: #6a59e0; }
QPushButton#heal:disabled { background: #44445a; color: #6c7086; }
QPushButton#clear { background: transparent; color: #6c7086; }
QPushButton#clear:hover { color: #cdd6f4; }
QCheckBox { color: #a6adc8; font-size: 12px; }
QCheckBox:disabled { color: #6c7086; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #6c7086; border-radius: 3px; background: #2a2a3e; }
QCheckBox::indicator:checked { background: #7c6af7; border-color: #7c6af7; }
QCheckBox::indicator:disabled { border-color: #44445a; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #1e1e2e; width: 6px; }
QScrollBar::handle:vertical { background: #44445a; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
"""


class HealWorker(QThread):
    progress = pyqtSignal(str, bool, str, str)

    def __init__(self, paths, blender, strip_floaters):
        super().__init__()
        self.paths = paths
        self.blender = blender
        self.strip_floaters = strip_floaters

    def run(self):
        for path in self.paths:
            try:
                r = heal_file(path, blender=self.blender, strip_floaters=self.strip_floaters)
            except Exception as e:
                self.progress.emit(path, False, "error", str(e))
                continue
            if r.ok:
                detail = f"{r.vertices:,}v  {r.faces:,}f"
                tip = f"Wrote {r.output}"
                if r.floaters_removed:
                    tip += f"\nRemoved {r.floaters_removed} floating piece(s)"
                self.progress.emit(path, True, detail, tip)
            else:
                self.progress.emit(path, False, (r.reason or "failed")[:30], r.reason or "")


class FileRow(QFrame):
    removed = pyqtSignal(object)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.setStyleSheet("QFrame { background: #2a2a3e; border-radius: 6px; }")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)

        name = QLabel(os.path.basename(path))
        name.setStyleSheet("color: #cdd6f4;")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(name)

        self._detail = QLabel("")
        self._detail.setStyleSheet("color: #6c7086; font-size: 11px; min-width: 130px;")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._detail)

        self._status = QLabel("queued")
        self._status.setStyleSheet("color: #6c7086; min-width: 70px;")
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._status)

        rm = QPushButton("x")
        rm.setStyleSheet(
            "color: #6c7086; background: transparent; font-size: 11px; padding: 2px 6px;"
        )
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(rm)

    def set_running(self):
        self._status.setText("healing...")
        self._status.setStyleSheet("color: #f9e2af; min-width: 70px;")

    def set_done(self, detail, tip=""):
        self._status.setText("done")
        self._status.setStyleSheet("color: #a6e3a1; min-width: 70px;")
        self._detail.setText(detail)
        self.setToolTip(tip)

    def set_error(self, detail, tip=""):
        self._status.setText("error")
        self._status.setStyleSheet("color: #f38ba8; min-width: 70px;")
        self._detail.setText(detail)
        self.setToolTip(tip)

    def is_done(self):
        return self._status.text() in ("done", "error")


class DropZone(QLabel):
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__("Drop OBJ / STL / PLY / GLB / glTF files here")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(80)
        self._update_style(False)

    def _update_style(self, active):
        border = "#7c6af7" if active else "#44445a"
        color = "#7c6af7" if active else "#6c7086"
        self.setStyleSheet(
            f"QLabel {{ border: 1.5px dashed {border}; border-radius: 8px; "
            f"color: {color}; background: #2a2a3e; padding: 18px; font-size: 13px; }}"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._update_style(True)

    def dragLeaveEvent(self, event):
        self._update_style(False)

    def dropEvent(self, event):
        self._update_style(False)
        paths = [
            u.toLocalFile() for u in event.mimeData().urls()
            if os.path.splitext(u.toLocalFile())[1].lower() in SUPPORTED
        ]
        if paths:
            self.files_dropped.emit(paths)


class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ObjectHealer")
        self.resize(560, 480)
        self.setStyleSheet(STYLE)
        self._rows: list[FileRow] = []
        self._worker = None
        try:
            self._blender, note = find_blender(), ""
        except BlenderNotFound as e:
            self._blender, note = None, str(e)
        self._build_ui()
        self._note.setText(note)
        self._note.setVisible(bool(note))
        self._heal_btn.setEnabled(self._blender is not None)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("ObjectHealer")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #cdd6f4;")
        sub = QLabel("mesh repair")
        sub.setStyleSheet("color: #6c7086; font-size: 12px; padding-top: 5px;")
        hdr.addWidget(title)
        hdr.addWidget(sub)
        hdr.addStretch()
        root.addLayout(hdr)

        self._drop = DropZone()
        self._drop.files_dropped.connect(self._add_files)
        root.addWidget(self._drop)

        browse = QPushButton("Browse files...")
        browse.setStyleSheet(
            "QPushButton { color: #7c6af7; background: transparent; "
            "font-size: 12px; padding: 2px; }"
            "QPushButton:hover { color: #cdd6f4; }"
        )
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._browse)
        browse.setFlat(True)
        root.addWidget(browse, alignment=Qt.AlignmentFlag.AlignHCenter)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch()
        scroll.setWidget(self._list_widget)
        root.addWidget(scroll)

        self._strip = QCheckBox("Strip floating pieces first (raw scans)")
        if not floaters.available():
            self._strip.setEnabled(False)
            self._strip.setToolTip(floaters.MISSING_EXTRA.replace("--strip-floaters", "This option"))
        root.addWidget(self._strip)

        self._note = QLabel("")
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: #f38ba8; font-size: 12px;")
        root.addWidget(self._note)

        bar = QHBoxLayout()
        clear = QPushButton("Clear done")
        clear.setObjectName("clear")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._clear_done)
        bar.addWidget(clear)
        bar.addStretch()

        self._heal_btn = QPushButton("Heal All")
        self._heal_btn.setObjectName("heal")
        self._heal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._heal_btn.clicked.connect(self._heal_all)
        bar.addWidget(self._heal_btn)
        root.addLayout(bar)

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select mesh files", "",
            "Mesh files (*.obj *.stl *.ply *.glb *.gltf);;All files (*.*)",
        )
        self._add_files(paths)

    def _add_files(self, paths):
        existing = {r.path for r in self._rows}
        for path in paths:
            if path not in existing and os.path.isfile(path):
                row = FileRow(path)
                row.removed.connect(self._remove_row)
                self._list_layout.insertWidget(self._list_layout.count() - 1, row)
                self._rows.append(row)
                existing.add(path)

    def _remove_row(self, row):
        if row in self._rows:
            self._rows.remove(row)
        row.setParent(None)

    def _clear_done(self):
        for row in [r for r in list(self._rows) if r.is_done()]:
            self._remove_row(row)

    def _heal_all(self):
        queued = [r for r in self._rows if r._status.text() == "queued"]
        if not queued or not self._blender or (self._worker and self._worker.isRunning()):
            return
        for row in queued:
            row.set_running()
        self._heal_btn.setEnabled(False)
        self._worker = HealWorker([r.path for r in queued], self._blender, self._strip.isChecked())
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(lambda: self._heal_btn.setEnabled(True))
        self._worker.start()

    def _on_progress(self, path, ok, detail, tip):
        for row in self._rows:
            if row.path == path:
                row.set_done(detail, tip) if ok else row.set_error(detail, tip)
                break


def app_icon():
    """The Dock and window icon, at the large size and the small one Qt uses in lists."""
    icon = QIcon()
    for size in (512, 32):
        icon.addFile(str(resources.files("object_healer") / "icons" / f"icon-{size}.png"),
                     QSize(size, size))
    return icon


def run_gui():
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ObjectHealer")
    app.setWindowIcon(app_icon())
    win = App()
    win.show()
    return app.exec()
