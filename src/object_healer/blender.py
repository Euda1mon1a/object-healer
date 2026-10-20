"""Find Blender and run the repair pass (heal.py) in it, one Blender process per file."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources

from . import gltf

# Exactly the formats heal.py imports and exports. Never .blend: a Blender file can carry
# Python (drivers, registered text blocks) that runs if auto-run is ever on.
SUPPORTED = (".obj", ".stl", ".ply", ".glb", ".gltf")
MAC_APP_PATHS = (
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "~/Applications/Blender.app/Contents/MacOS/Blender",
)
RESULT_PREFIX = "OBJECT_HEALER_RESULT "
DEFAULT_TIMEOUT = 300

NOT_FOUND = (
    "Blender was not found. Install Blender 4.0 or later from https://www.blender.org/download/ "
    "or set BLENDER to its executable (on a Mac, /Applications/Blender.app/Contents/MacOS/Blender)."
)


class BlenderNotFound(RuntimeError):
    pass


def _executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_blender(env=None, which=shutil.which, app_paths=MAC_APP_PATHS):
    """$BLENDER, then the usual Blender.app locations, then `blender` on PATH."""
    env = os.environ if env is None else env
    configured = env.get("BLENDER")
    if configured:
        path = os.path.expanduser(configured)
        if _executable(path):
            return path
        raise BlenderNotFound(f"BLENDER is set to {configured!r}, which is not an executable file.")
    for path in app_paths:
        path = os.path.expanduser(path)
        if _executable(path):
            return path
    found = which("blender")
    if found:
        return found
    raise BlenderNotFound(NOT_FOUND)


def heal_script():
    return str(resources.files("object_healer") / "heal.py")


def default_output(path):
    stem, ext = os.path.splitext(path)
    return stem + "_healed" + ext


@dataclass
class HealResult:
    ok: bool
    input: str
    output: str | None = None
    vertices: int | None = None
    faces: int | None = None
    reason: str | None = None
    floaters_removed: int | None = None


def check_paths(src, dst):
    """Return a reason the pair can't be healed, or None."""
    if not os.path.isfile(src):
        return "no such file"
    if any(os.path.splitext(p)[1].lower() in (".blend", ".blend1") for p in (src, dst)):
        return "Blender files (.blend) are refused: they can carry embedded Python"
    for path, what in ((src, "input"), (dst, "output")):
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED:
            return f"unsupported {what} format {ext or '(none)'}; use one of {', '.join(SUPPORTED)}"
    if os.path.realpath(src) == os.path.realpath(dst):
        return "the output would overwrite the input"
    if not os.path.isdir(os.path.dirname(dst)):
        return f"the output folder {os.path.dirname(dst)} does not exist"
    return None


def run_blender(src, dst, blender, timeout=DEFAULT_TIMEOUT, run=subprocess.run, *, cwd):
    """Run heal.py in Blender with factory settings (no user add-ons or startup scripts) and
    auto-run of embedded Python off, from cwd, a folder object-healer created."""
    cmd = [blender, "--background", "--factory-startup", "--disable-autoexec",
           "--python-exit-code", "1", "--python", heal_script(), "--", src, "--output", dst]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return HealResult(False, src, reason=f"Blender took longer than {timeout:g} s")
    except OSError as e:
        return HealResult(False, src, reason=f"could not start Blender: {e}")
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            r = json.loads(line[len(RESULT_PREFIX):])
            return HealResult(r["status"] == "ok", src, r["output"], r["vertices"], r["faces"], r["reason"])
    tail = [l for l in proc.stderr.splitlines() if l.strip()] or \
           [l for l in proc.stdout.splitlines() if l.strip()]
    return HealResult(False, src, reason=f"Blender exited with status {proc.returncode} and no result"
                      + (f": {tail[-1].strip()}" if tail else ""))


def strip_in_subprocess(src, dst, timeout=DEFAULT_TIMEOUT, run=subprocess.run, *, cwd):
    """Run floaters.strip_floaters in its own Python (PyMeshLab's Qt 5 must stay out of the
    window's process); return the number of pieces removed or raise RuntimeError.

    -I keeps the working directory, PYTHONPATH and user site-packages off sys.path, so a
    planted object_healer/ folder can't stand in for the installed package; cwd is a folder
    object-healer created as well.
    """
    try:
        proc = run([sys.executable, "-I", "-m", "object_healer.floaters", src, dst],
                   capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"took longer than {timeout:g} s") from None
    for line in reversed(proc.stdout.splitlines()):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and "removed" in r:
            return r["removed"]
        if isinstance(r, dict) and "error" in r:
            raise RuntimeError(r["error"])
    tail = [l for l in proc.stderr.splitlines() if l.strip()]
    raise RuntimeError(f"exited with status {proc.returncode}" + (f": {tail[-1].strip()}" if tail else ""))


def heal_file(src, dst=None, *, blender, strip_floaters=False, timeout=DEFAULT_TIMEOUT,
              run=subprocess.run):
    """Heal one mesh into dst (default <name>_healed.<ext> next to src)."""
    src = os.path.abspath(src)
    dst = os.path.abspath(dst or default_output(src))
    problem = check_paths(src, dst)
    if problem:
        return HealResult(False, src, reason=problem)

    with tempfile.TemporaryDirectory(prefix="object-healer-") as tmp:
        source = src
        if os.path.splitext(src)[1].lower() in (".gltf", ".glb"):
            # Blender (and PyMeshLab) get a private copy holding only the files the model may
            # reference, so nothing outside its folder is read and nothing can be swapped in.
            try:
                source = gltf.stage(src, os.path.join(tmp, "model"))
            except gltf.Refused as e:
                return HealResult(False, src, reason=str(e))
        removed = None
        if strip_floaters:
            stripped = os.path.join(tmp, "stripped.ply")
            try:
                removed = strip_in_subprocess(source, stripped, timeout, run, cwd=tmp)
            except (RuntimeError, OSError) as e:
                return HealResult(False, src, reason=f"floater removal failed: {e}")
            source = stripped
        result = run_blender(source, dst, blender, timeout, run, cwd=tmp)
    result.input = src
    result.floaters_removed = removed
    return result
