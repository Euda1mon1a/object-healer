<p align="center"><img src="https://raw.githubusercontent.com/Euda1mon1a/object-healer/main/docs/icon/icon-256.png" width="128" alt="ObjectHealer icon"></p>

# ObjectHealer

ObjectHealer repairs meshes for 3D printing. Give it an OBJ, STL, PLY, GLB or glTF file and
it writes a copy with duplicate vertices merged, normals pointing outward and holes filled, so
the slicer gets a closed solid. The repair runs in Blender with no window open, from a
drag-and-drop window or from the command line.

It started as the repair step of [Splinter](https://github.com/Euda1mon1a/splinter), a mesh
segmentation tool, and became its own tool because most scanned or generated meshes need
repair whether or not they get segmented.

## Requirements

- **Blender 4.0 or later.** Verified with Blender 5.1.0 on macOS 27 (Apple silicon). Older
  versions lack the import and export operators it uses. ObjectHealer looks for Blender in this
  order:
  1. the `BLENDER` environment variable;
  2. `/Applications/Blender.app`, then `~/Applications/Blender.app`;
  3. `blender` on your PATH.
- **Python 3.10 or later.** Tested on 3.10, 3.12 and 3.14.

## Install

```bash
uv tool install object-healer
# or
pipx install object-healer
```

Raw scans usually need floater removal as well. It comes with the `scan` extra, which adds
PyMeshLab:

```bash
uv tool install 'object-healer[scan]'
# or
pipx install 'object-healer[scan]'
```

## Use

### Window

```bash
object-healer
```

Drop files on the window, or use **Browse files...**, then click **Heal All**. Each healed copy
is written next to its original as `<name>_healed.<ext>`. Hover over a finished row for the
output path or the full error. Tick **Strip floating pieces first** for raw scans; the option
is greyed out until the `scan` extra is installed.

### Command line

```bash
object-healer heal part.stl scan.ply model.glb     # each to <name>_healed.<ext>
object-healer heal model.obj -o model.stl          # name the output; its extension picks the format
object-healer heal --strip-floaters raw_scan.ply   # remove scan debris first
```

Other options: `--blender PATH` picks a specific Blender, and `--timeout SECONDS` sets the limit
for each file (default 300). An existing output is overwritten; the input never is. The exit
status is 0 when every file healed, 1 when any file failed, and 2 for a usage or setup error,
such as no Blender or a missing extra.

## What the repair does

Each file gets its own Blender process, started with factory settings so your add-ons and
preferences don't change the result. The file is imported, every mesh in it is joined into one
object, and one Edit-mode pass runs:

| Step | Blender operator | Fixes |
|------|------------------|-------|
| Merge by distance | `mesh.remove_doubles(threshold=0.0001)` | duplicate vertices, such as an STL's separate corners for every triangle |
| Recalculate normals | `mesh.normals_make_consistent(inside=False)` | flipped and inside-out faces |
| Fill holes | `mesh.fill_holes(sides=0)` | open boundaries of any size, closed so the mesh is watertight |

**Floater removal** (`--strip-floaters`) runs before Blender. Blender's hole filling closes the
object but keeps every disconnected piece, and a raw scan often carries specks of turntable and
background. PyMeshLab removes each piece whose bounding-box diagonal is under 5% of the biggest
piece's diagonal. The threshold is taken from the biggest piece rather than the whole file on
purpose: MeshLab's own percentage is of the whole file's bounding box, so a single speck far
from the object raises the threshold until it deletes the object too.

## Limits

- It closes holes and fixes normals. It does not repair self-intersections, edges shared by
  three or more faces, or walls too thin to print.
- Hole filling closes every open boundary, including openings you meant to keep, so an open
  shell such as a single-surface cup comes back closed.
- Each filled hole becomes one polygon. STL and GLB output is triangulated, but OBJ and PLY
  output keeps that polygon for the next tool to triangulate.
- Duplicate vertices within 0.0001 units are merged. For a model in millimetres that is
  0.1 µm; for a model in metres it is 0.1 mm.
- Materials from an OBJ's `.mtl` are dropped on import. An `.mtl` can name any image on disk,
  and GLB output would embed it. OBJ output never carries materials.
- With `--strip-floaters`, the mesh passes through PLY on its way to Blender, so UVs and
  materials from the input are lost. Scans rarely carry either.
- `--strip-floaters` needs faces, so it refuses a point cloud.
- Blender files (`.blend`) are refused, because they can carry embedded Python. Blender runs
  with factory settings and with auto-run of embedded scripts off.
- A glTF or GLB may refer to other files only through base64 `data:` URIs or relative paths
  that stay inside its own folder, with symlinks followed. Absolute paths, `..` escapes and
  `file:` or web URLs are refused. Blender then gets a private copy of the model and of those
  files only, so a crafted model can't pull anything else into the output.

## License

GPL-3.0-or-later. Copyright 2026 Aaron Montgomery. The repair script runs inside Blender
through Blender's Python API, and Blender's license asks that such scripts be shared under the
GPL. The window uses PyQt6, which is GPL as well. See [LICENSE](LICENSE).

Provided as-is; bug reports are welcome, fixes are not guaranteed.
