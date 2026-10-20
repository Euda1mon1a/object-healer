import numpy as np
import pytest

from object_healer.blender import BlenderNotFound, find_blender


@pytest.fixture(scope="session")
def blender():
    """The real Blender, or skip."""
    try:
        return find_blender()
    except BlenderNotFound:
        pytest.skip("Blender is not installed")


@pytest.fixture
def broken_cube(tmp_path):
    """A 10 mm cube saved with one side missing and every triangle carrying its own corners,
    so the healer has duplicate vertices to merge and a hole to fill."""
    trimesh = pytest.importorskip("trimesh")
    cube = trimesh.creation.box(extents=(10, 10, 10))
    faces = cube.faces[2:]
    soup = trimesh.Trimesh(cube.vertices[faces.reshape(-1)],
                           np.arange(faces.size).reshape(-1, 3), process=False)
    path = tmp_path / "broken.obj"
    soup.export(path)
    return path


@pytest.fixture
def scan_with_floaters(tmp_path):
    """A 10 mm cube with one side missing plus three specks of debris, one of them far off."""
    trimesh = pytest.importorskip("trimesh")
    cube = trimesh.creation.box(extents=(10, 10, 10))
    pieces = [trimesh.Trimesh(cube.vertices, cube.faces[2:], process=False)]
    for offset in ((8, 0, 0), (0, 9, 0), (400, 0, 0)):
        speck = trimesh.creation.box(extents=(0.2, 0.2, 0.2))
        speck.apply_translation(offset)
        pieces.append(speck)
    path = tmp_path / "scan.ply"
    trimesh.util.concatenate(pieces).export(path)
    return path
