import sys

import numpy as np
import pytest

from object_healer import floaters


def test_largest_piece_diagonal():
    # a 3-4-0 triangle (diagonal 5) touching a second triangle through a shared vertex, and a
    # separate tiny triangle; unreferenced vertex 9 at the far corner must not count
    v = np.array([[0, 0, 0], [3, 0, 0], [0, 4, 0], [3, 4, 0],
                  [50, 50, 50], [50.1, 50, 50], [50, 50.1, 50],
                  [0, 0, 0], [0, 0, 0], [999, 999, 999]], dtype=float)
    f = np.array([[0, 1, 2], [1, 3, 2], [4, 5, 6]])
    assert floaters.largest_piece_diagonal(v, f) == pytest.approx(5.0)


def test_largest_piece_diagonal_long_chain():
    # a strip of 2,000 triangles is one piece however its indices are ordered
    n = 2000
    v = np.array([[i, i % 2, 0] for i in range(n + 2)], dtype=float)
    f = np.array([[i, i + 1, i + 2] for i in range(n)])[np.random.default_rng(1).permutation(n)]
    assert floaters.largest_piece_diagonal(v, f) == pytest.approx(np.hypot(n + 1, 1))


def test_missing_extra(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "pymeshlab", None)
    assert not floaters.available()
    with pytest.raises(floaters.MissingExtra, match=r"object-healer\[scan\]"):
        floaters.strip_floaters(str(tmp_path / "a.ply"), str(tmp_path / "b.ply"))


def components(path):
    pymeshlab = pytest.importorskip("pymeshlab")
    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(str(path))
    return ms.get_topological_measures()["connected_components_number"], ms.current_mesh()


def test_strip_keeps_the_object_and_drops_debris(scan_with_floaters, tmp_path):
    pytest.importorskip("pymeshlab")
    assert components(scan_with_floaters)[0] == 4
    out = tmp_path / "stripped.ply"
    assert floaters.strip_floaters(str(scan_with_floaters), str(out)) == 3
    n, mesh = components(out)
    # one speck sat 400 mm away: MeshLab's whole-mesh percentage would have deleted the cube
    assert n == 1
    assert mesh.bounding_box().diagonal() == pytest.approx(np.sqrt(300))


def test_strip_keeps_real_second_parts(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("pymeshlab")
    a = trimesh.creation.box(extents=(10, 10, 10))
    b = trimesh.creation.box(extents=(2, 2, 2))  # 20% of the big one: a part, not debris
    b.apply_translation((30, 0, 0))
    src, out = tmp_path / "two.ply", tmp_path / "two_out.ply"
    trimesh.util.concatenate([a, b]).export(src)
    assert floaters.strip_floaters(str(src), str(out)) == 0
    assert components(out)[0] == 2


def test_point_cloud_is_refused(tmp_path):
    pytest.importorskip("pymeshlab")
    src = tmp_path / "cloud.ply"
    src.write_text("ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\n"
                   "property float z\nend_header\n0 0 0\n1 0 0\n0 1 0\n")
    with pytest.raises(ValueError, match="no faces"):
        floaters.strip_floaters(str(src), str(tmp_path / "out.ply"))


def test_main_prints_the_count(scan_with_floaters, tmp_path, capsys):
    pytest.importorskip("pymeshlab")
    assert floaters.main([str(scan_with_floaters), str(tmp_path / "out.ply")]) == 0
    assert capsys.readouterr().out.strip().endswith('{"removed": 3}')


def test_main_reports_errors_as_json(tmp_path, capsys):
    assert floaters.main(["only-one"]) == 2
    assert '"error": "usage' in capsys.readouterr().out
    pytest.importorskip("pymeshlab")
    assert floaters.main([str(tmp_path / "missing.ply"), str(tmp_path / "out.ply")]) == 1
    assert '"error"' in capsys.readouterr().out


def test_available_does_not_import_pymeshlab():
    # PyMeshLab's bundled Qt 5 must never load next to the window's PyQt6 (Qt 6)
    import subprocess
    code = ("import sys; from object_healer import floaters, gui; floaters.available(); "
            "print('pymeshlab' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
