"""End to end through the real Blender. Skipped when Blender isn't installed."""
import pytest

from object_healer import cli
from object_healer.blender import heal_file

trimesh = pytest.importorskip("trimesh")


def load(path):
    """Load with vertices merged by position: OBJ and glTF readers split corners by normal."""
    mesh = trimesh.load(str(path), force="mesh")
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    return mesh


def closed(path):
    if str(path).endswith(".ply"):
        # Blender writes the filled hole as one polygon; trimesh can't read mixed-size PLY faces
        pymeshlab = pytest.importorskip("pymeshlab")
        ms = pymeshlab.MeshSet()
        ms.load_new_mesh(str(path))
        t = ms.get_topological_measures()
        return t["boundary_edges"] == 0 and t["is_mesh_two_manifold"]
    return load(path).is_watertight


def test_broken_cube_comes_back_watertight(blender, broken_cube):
    assert not closed(broken_cube)
    r = heal_file(str(broken_cube), blender=blender)
    assert r.ok, r.reason
    assert r.output == str(broken_cube.with_name("broken_healed.obj"))
    assert r.vertices == 8  # 30 split corners merged back to the cube's 8
    healed = load(r.output)
    assert healed.is_watertight
    assert healed.volume == pytest.approx(1000, rel=1e-6)


@pytest.mark.parametrize("ext", [".stl", ".ply", ".glb"])
def test_output_extension_picks_the_format(blender, broken_cube, ext):
    out = broken_cube.with_name("converted" + ext)
    r = heal_file(str(broken_cube), str(out), blender=blender)
    assert r.ok, r.reason
    assert closed(out)


def test_cli_end_to_end(blender, broken_cube, capsys, monkeypatch):
    monkeypatch.setenv("BLENDER", blender)
    assert cli.main(["heal", str(broken_cube)]) == 0
    assert "8 vertices" in capsys.readouterr().out
    assert closed(broken_cube.with_name("broken_healed.obj"))


def test_a_file_without_a_mesh_fails_cleanly(blender, tmp_path, capsys, monkeypatch):
    junk = tmp_path / "junk.obj"
    junk.write_text("# not a mesh\n")
    monkeypatch.setenv("BLENDER", blender)
    assert cli.main(["heal", str(junk)]) == 1
    assert "no mesh in the file" in capsys.readouterr().err
    assert not junk.with_name("junk_healed.obj").exists()


def test_without_stripping_the_debris_stays(blender, scan_with_floaters):
    out = scan_with_floaters.with_name("kept.stl")
    r = heal_file(str(scan_with_floaters), str(out), blender=blender)
    assert r.ok, r.reason
    assert len(load(out).split(only_watertight=False)) == 4


def test_strip_floaters_end_to_end(blender, scan_with_floaters):
    pytest.importorskip("pymeshlab")
    out = scan_with_floaters.with_name("clean.stl")
    r = heal_file(str(scan_with_floaters), str(out), blender=blender, strip_floaters=True)
    assert r.ok, r.reason
    assert r.floaters_removed == 3
    mesh = load(out)
    assert mesh.is_watertight
    assert len(mesh.split(only_watertight=False)) == 1


def test_blender_ignores_modules_planted_in_the_working_directory(blender, broken_cube, tmp_path,
                                                                 monkeypatch):
    marker = tmp_path / "PLANTED-MODULE-RAN"
    for name in ("json", "argparse", "os"):
        (tmp_path / f"{name}.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    monkeypatch.chdir(tmp_path)
    r = heal_file(str(broken_cube), blender=blender)
    assert r.ok, r.reason
    assert not marker.exists()
