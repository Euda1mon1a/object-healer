"""
Blender-side repair pass for ObjectHealer. It runs inside Blender, not in a normal Python:

    blender --background --factory-startup --python heal.py -- IN [IN ...]
    blender --background --factory-startup --python heal.py -- IN --output OUT

Each input is imported into an empty scene, its meshes are joined, and one Edit-mode pass
merges duplicate vertices, makes normals consistent and fills holes. The result goes to OUT,
or to <name>_healed.<ext> next to the input; OUT's extension picks the export format.

For each file it prints a line for people (OK:<out>:<verts>:<faces>, ERROR:<path>:<reason> or
SKIP:<path>:<reason>) and one for the object-healer command (OBJECT_HEALER_RESULT <json>).
The exit status is 1 if any file failed.

Requires Blender 4.0+ (uses the core import/export operators that replaced the old
io_mesh_* add-ons). On Blender 5.x the STL importer/exporter is `wm.stl_import` /
`wm.stl_export` (the pre-4.0 `import_mesh.stl` / `export_mesh.stl` are gone) and the
glTF exporter is `export_scene.gltf` (not the old `export_scene.gltf2`).
"""
import argparse
import json
import os
import sys

import bpy

MIN_BLENDER = (4, 0, 0)
RESULT_PREFIX = "OBJECT_HEALER_RESULT "

IMPORTERS = {
    ".obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
    ".stl": lambda p: bpy.ops.wm.stl_import(filepath=p),
    ".ply": lambda p: bpy.ops.wm.ply_import(filepath=p),
    ".glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
    ".gltf": lambda p: bpy.ops.import_scene.gltf(filepath=p),
}

EXPORTERS = {
    ".obj": lambda p: bpy.ops.wm.obj_export(
        filepath=p, export_selected_objects=True,
        export_materials=False, forward_axis="NEGATIVE_Z", up_axis="Y",
    ),
    ".stl": lambda p: bpy.ops.wm.stl_export(filepath=p, export_selected_objects=True),
    ".ply": lambda p: bpy.ops.wm.ply_export(filepath=p, export_selected_objects=True),
    ".glb": lambda p: bpy.ops.export_scene.gltf(filepath=p, use_selection=True, export_format="GLB"),
    ".gltf": lambda p: bpy.ops.export_scene.gltf(
        filepath=p, use_selection=True, export_format="GLTF_SEPARATE",
    ),
}


def report(status, path, out=None, verts=None, faces=None, reason=None):
    if status == "ok":
        print(f"OK:{out}:{verts}:{faces}", flush=True)
    else:
        print(f"{status.upper()}:{path}:{reason}", flush=True)
    result = {"status": status, "input": path, "output": out,
              "vertices": verts, "faces": faces, "reason": reason}
    print(RESULT_PREFIX + json.dumps(result), flush=True)


def default_output(input_path):
    stem, ext = os.path.splitext(input_path)
    return stem + "_healed" + ext


def heal(input_path, output_path):
    in_ext = os.path.splitext(input_path)[1].lower()
    out_ext = os.path.splitext(output_path)[1].lower()
    if in_ext not in IMPORTERS:
        report("skip", input_path, reason=f"unsupported format {in_ext or '(none)'}")
        return False
    if out_ext not in EXPORTERS:
        report("skip", input_path, reason=f"unsupported output format {out_ext or '(none)'}")
        return False

    bpy.ops.wm.read_factory_settings(use_empty=True)
    IMPORTERS[in_ext](input_path)

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        report("error", input_path, reason="no mesh in the file")
        return False

    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()

    obj = bpy.context.active_object
    if in_ext == ".obj":
        # The importer has no switch for this: an OBJ's .mtl can name any image on disk, and
        # glTF export would embed it in the output. Materials are dropped instead.
        obj.data.materials.clear()

    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.0001)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.mesh.fill_holes(sides=0)
    bpy.ops.object.mode_set(mode="OBJECT")

    EXPORTERS[out_ext](output_path)

    report("ok", input_path, out=output_path,
           verts=len(obj.data.vertices), faces=len(obj.data.polygons))
    return True


def main(argv):
    parser = argparse.ArgumentParser(prog="heal.py")
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", help="output path; only with a single input")
    args = parser.parse_args(argv)
    if args.output and len(args.inputs) > 1:
        parser.error("--output takes a single input")

    if bpy.app.version < MIN_BLENDER:
        found = ".".join(str(n) for n in bpy.app.version)
        for path in args.inputs:
            report("error", path, reason=f"needs Blender 4.0 or later, found {found}")
        return 1

    failed = False
    for path in args.inputs:
        try:
            ok = heal(path, args.output or default_output(path))
        except Exception as e:
            report("error", path, reason=" ".join(str(e).split()) or type(e).__name__)
            ok = False
        failed = failed or not ok
    return 1 if failed else 0


argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
sys.exit(main(argv))
