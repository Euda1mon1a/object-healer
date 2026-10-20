"""Optional pre-pass for raw scans: drop small disconnected pieces (turntable and background
debris) with PyMeshLab before the Blender pass. Blender's hole filling closes the object but
keeps every floating piece, so scans that include their surroundings need this first.

PyMeshLab bundles its own Qt 5, which must not load into a process that already has PyQt6's
Qt 6 (the window), so object-healer runs this module in a subprocess:

    python -m object_healer.floaters SRC DST    # prints {"removed": N} or {"error": "..."}
"""
import importlib.util
import json
import sys

MISSING_EXTRA = (
    "--strip-floaters needs PyMeshLab, which ships with the scan extra: "
    "uv tool install 'object-healer[scan]' (or pipx install 'object-healer[scan]')"
)
DEFAULT_PERCENT = 5.0


class MissingExtra(RuntimeError):
    pass


def available():
    """Whether PyMeshLab is installed, without importing it (see the module docstring)."""
    return importlib.util.find_spec("pymeshlab") is not None


def _components(ms):
    return ms.get_topological_measures()["connected_components_number"]


def largest_piece_diagonal(vertices, faces):
    """Bounding-box diagonal of the biggest piece, where faces sharing a vertex join a piece.

    Union-find in numpy (hook roots to the smaller root, then compress), so the cost tracks
    the face count rather than the number of pieces.
    """
    import numpy as np

    parent = np.arange(len(vertices))
    a = np.concatenate([faces[:, 0], faces[:, 1]])
    b = np.concatenate([faces[:, 1], faces[:, 2]])
    while True:
        ra, rb = parent[a], parent[b]
        if (ra == rb).all():
            break
        low = np.minimum(ra, rb)
        np.minimum.at(parent, ra, low)
        np.minimum.at(parent, rb, low)
        while True:
            jumped = parent[parent]
            if (jumped == parent).all():
                break
            parent = jumped

    used = np.unique(faces)
    labels, points = parent[used], vertices[used]
    order = np.argsort(labels, kind="stable")
    labels, points = labels[order], points[order]
    starts = np.flatnonzero(np.r_[True, labels[1:] != labels[:-1]])
    lo = np.minimum.reduceat(points, starts, axis=0)
    hi = np.maximum.reduceat(points, starts, axis=0)
    return float(np.sqrt(((hi - lo) ** 2).sum(axis=1)).max())


def strip_floaters(src, dst, percent=DEFAULT_PERCENT):
    """Write src to dst without pieces smaller than percent of the biggest piece; return how
    many pieces were removed.

    The size is each piece's bounding-box diagonal. The threshold is taken from the biggest
    piece, not from the whole mesh: MeshLab's own percentage is of the whole mesh's bounding
    box, so one far-off speck can make it delete the object itself.
    """
    try:
        import pymeshlab
    except ImportError:
        raise MissingExtra(MISSING_EXTRA) from None

    ms = pymeshlab.MeshSet()
    ms.load_new_mesh(src)
    mesh = ms.current_mesh()
    if mesh.face_number() == 0:
        raise ValueError("the file has no faces (a point cloud?), so there are no pieces to compare")
    before = _components(ms)
    if before > 1:
        threshold = largest_piece_diagonal(mesh.vertex_matrix(), mesh.face_matrix()) * percent / 100
        ms.meshing_remove_connected_component_by_diameter(
            mincomponentdiag=pymeshlab.PureValue(threshold))
    removed = before - _components(ms)
    ms.save_current_mesh(dst)
    return removed


def main(argv):
    if len(argv) != 2:
        print(json.dumps({"error": "usage: python -m object_healer.floaters SRC DST"}))
        return 2
    try:
        removed = strip_floaters(*argv)
    except Exception as e:
        print(json.dumps({"error": " ".join(str(e).split()) or type(e).__name__}))
        return 1
    print(json.dumps({"removed": removed}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
