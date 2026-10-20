import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

from object_healer import gui  # noqa: E402
from object_healer.blender import BlenderNotFound, HealResult  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_without_blender_the_window_says_so(qapp, monkeypatch):
    def missing():
        raise BlenderNotFound("Blender was not found. Install Blender 4.0 or later")
    monkeypatch.setattr(gui, "find_blender", missing)
    win = gui.App()
    assert not win._heal_btn.isEnabled()
    assert "Install Blender" in win._note.text()


def test_rows_and_healing(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "find_blender", lambda: "/bin/blender")
    win = gui.App()
    assert win._heal_btn.isEnabled() and not win._note.text()
    good, bad = tmp_path / "a.stl", tmp_path / "b.stl"
    good.write_text("x")
    bad.write_text("x")
    win._add_files([str(good), str(bad), str(good), str(tmp_path / "missing.stl")])
    assert [r.path for r in win._rows] == [str(good), str(bad)]

    def fake_heal(path, *, blender, strip_floaters):
        if path == str(bad):
            return HealResult(False, path, reason="no mesh in the file")
        return HealResult(True, path, path + ".out", 8, 11)

    monkeypatch.setattr(gui, "heal_file", fake_heal)
    worker = gui.HealWorker([str(good), str(bad)], "/bin/blender", False)
    worker.progress.connect(win._on_progress)
    worker.run()  # synchronously, on this thread
    statuses = [(r._status.text(), r._detail.text()) for r in win._rows]
    assert statuses == [("done", "8v  11f"), ("error", "no mesh in the file")]
    win._clear_done()
    assert win._rows == []


def test_app_icon_loads_both_sizes(qapp):
    icon = gui.app_icon()
    assert not icon.isNull()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(512, 512), (32, 32)} <= sizes
    assert not icon.pixmap(512, 512).isNull()
