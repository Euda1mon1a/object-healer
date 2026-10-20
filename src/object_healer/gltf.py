"""Stage a glTF or GLB, and only the files it may reference, into a private folder for Blender.

A .gltf (and the JSON chunk of a .glb) names its buffers and images by URI, and Blender's
importer reads whatever they resolve to. A crafted model could otherwise pull a local file's
bytes into the healed output. So before Blender sees anything:

- every buffers[].uri and images[].uri must be a base64 data: URI or a relative path that stays
  inside the model's own folder, judged with Blender's own rules (below), both on the path as
  written and after symlinks are followed;
- the model and each file it references are copied into a private folder, and Blender is given
  only the copy. Each referenced file is read through one descriptor; the kernel is asked where
  that open file actually is (F_GETPATH), which must be inside the model's folder, and the name
  must still lead to the same file (device and inode). A file swapped while it is checked, or a
  folder above it swapped for a symlink, is refused rather than copied.
- a referenced file must be a regular file with no other hard links (its location could not be
  confirmed) and not sparse, and the referenced files together may total at most
  MAX_REFERENCED_BYTES, so a model can't make staging fill the disk. Anything that goes wrong
  while staging, out of disk space included, is a refusal for that file.

Blender 5.1 (io_scene_gltf2, io/imp/gltf2_io_gltf.py load_uri) decodes a URI as data only when
it starts with "data:" and contains ";base64,"; anything else, including "data:/../x", is
read as a file at join(dirname(model), uri_to_path(uri)), where uri_to_path turns "\\" into "/",
percent-decodes and normpaths (io/com/path.py). The data test here is stricter than Blender's,
so whatever passes it Blender also decodes as data.
"""
import json
import os
import re
import stat
import struct
from urllib.parse import unquote, urlsplit

try:
    import fcntl
except ImportError:  # not on Windows; there /proc isn't either, so references are refused
    fcntl = None

GLB_MAGIC = b"glTF"
GLB_JSON_CHUNK = 0x4E4F534A
DATA_URI = re.compile(r"data:[^,]*;base64,")
MAX_REFERENCED_BYTES = 1 << 30  # 1 GiB for all of a model's referenced files together
SPARSE_MIN_BYTES = 1 << 20  # below this, block rounding makes the allocation test meaningless
PROC_FD = "/proc/self/fd"


class Refused(Exception):
    pass


def uri_to_path(uri):
    """Blender's io_scene_gltf2 uri_to_path, verbatim in effect."""
    return os.path.normpath(unquote(uri.replace("\\", "/")))


def _json_from(data, glb):
    if glb:
        if len(data) < 20 or data[:4] != GLB_MAGIC:
            raise ValueError("not a GLB file")
        length, kind = struct.unpack_from("<II", data, 12)
        if kind != GLB_JSON_CHUNK or 20 + length > len(data):
            raise ValueError("the GLB has no JSON chunk")
        data = data[20:20 + length]
    doc = json.loads(data.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("the JSON is not an object")
    return doc


def _uris(doc):
    for key in ("buffers", "images"):
        items = doc.get(key) or []
        if not isinstance(items, list):
            raise ValueError(f"{key} is not a list")
        for i, item in enumerate(items):
            if isinstance(item, dict) and "uri" in item:
                yield f"{key}[{i}].uri", item["uri"]


def _inside(folder, path):
    return os.path.commonpath([folder, path]) == folder


def reference(uri, folder, real_folder=None):
    """The path, relative to folder, that Blender would read for uri; None for embedded data.
    Raises Refused naming the problem. real_folder is realpath(folder), if already known."""
    if not isinstance(uri, str):
        raise Refused("is not a string")
    if uri == "" or DATA_URI.match(uri):
        return None  # Blender skips an empty buffer URI; base64 data is decoded, not read
    if uri.startswith("data:"):
        raise Refused("is a data: URI without ;base64, (Blender would read it as a file path)")
    if urlsplit(uri).scheme:
        raise Refused("uses a URI scheme; only base64 data: URIs and files next to the model are allowed")
    rel = uri_to_path(uri)
    if "\x00" in rel:
        raise Refused("contains a NUL character")
    if os.path.isabs(rel):
        raise Refused("is an absolute path")
    lexical = os.path.normpath(os.path.join(folder, rel))
    real_folder = real_folder or os.path.realpath(folder)
    if not _inside(folder, lexical) or rel in (os.curdir, os.pardir):
        raise Refused("points outside the model's folder")
    if not _inside(real_folder, os.path.realpath(lexical)):
        raise Refused("points outside the model's folder (through a symlink)")
    return rel


def kernel_path(fd):
    """Where the kernel says the open file or folder fd is now, symlinks resolved."""
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):  # macOS
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
    if os.path.isdir(PROC_FD):  # Linux
        return os.readlink(os.path.join(PROC_FD, str(fd)))
    raise Refused("can't be confirmed to be inside the model's folder on this system")


def folder_kernel_path(folder):
    fd = os.open(folder, os.O_RDONLY)
    try:
        return kernel_path(fd)
    finally:
        os.close(fd)


def _is_sparse(st):
    if st.st_size < SPARSE_MIN_BYTES or getattr(st, "st_flags", 0) & getattr(stat, "UF_COMPRESSED", 0):
        return False  # tiny, or compressed by the file system (APFS/HFS+), which isn't sparse
    return st.st_blocks * 512 * 4 < st.st_size


def _copy_bounded(f, out, limit):
    copied = 0
    while chunk := f.read(1 << 20):
        copied += len(chunk)
        if copied > limit:
            raise Refused("grew past the size limit while it was being copied")
        out.write(chunk)
    return copied


def _copy_inside(folder, rel, dest, kernel_folder, budget):
    """Copy folder/rel to dest through one descriptor and return the bytes copied. The open file
    must be a regular, unshared, non-sparse file within budget, the kernel must place it inside
    kernel_folder, and the name must still lead to it."""
    path = os.path.join(folder, rel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Refused("names a file that does not exist") from None
    except OSError as e:
        raise Refused(f"can't be opened ({e.strerror})") from None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise Refused("is not a regular file")
        if opened.st_nlink > 1:
            raise Refused("has other hard links, so where it really lives can't be confirmed")
        if opened.st_size > budget:
            raise Refused(f"would take the model's referenced files past the "
                          f"{MAX_REFERENCED_BYTES >> 30} GiB limit")
        if _is_sparse(opened):
            raise Refused("is a sparse file (mostly holes), refused so staging can't fill the disk")
        if not _inside(kernel_folder, kernel_path(fd)):
            raise Refused("is open outside the model's folder (a folder on its path was swapped)")
        try:
            now = os.stat(os.path.realpath(path))
        except OSError:
            now = None
        if now is None or (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino):
            raise Refused("changed or moved outside the model's folder while it was being read")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            out = open(dest, "xb")
        except FileExistsError:
            return 0  # the same file under another case on a case-insensitive disk
        with out, os.fdopen(os.dup(fd), "rb") as f:
            return _copy_bounded(f, out, budget)
    finally:
        os.close(fd)


def stage(src, dest):
    """Copy the model at src and every file it references into the new folder dest, keeping the
    relative layout; return the copy of the model. Raises Refused with a reason for the user."""
    glb = src.lower().endswith(".glb")
    folder = os.path.dirname(os.path.abspath(src))
    try:
        real_folder = os.path.realpath(folder)
        with open(src, "rb") as f:
            data = f.read()
    except (OSError, ValueError) as e:
        raise Refused(f"the model could not be read ({e})") from None
    try:
        doc = _json_from(data, glb)
        uris = list(_uris(doc))
    except (ValueError, UnicodeDecodeError) as e:
        raise Refused(f"not a readable glTF file ({e})") from None

    refs = {}
    for label, uri in uris:
        try:
            rel = reference(uri, folder, real_folder)
        except Refused as e:
            raise Refused(f"refused: {label} {uri!r} {e}") from None
        except ValueError as e:  # a path os functions reject outright
            raise Refused(f"refused: {label} {uri!r} is not a usable path ({e})") from None
        if rel is not None:
            refs.setdefault(rel, (label, uri))

    staged = os.path.join(dest, os.path.basename(src))
    budget = MAX_REFERENCED_BYTES
    try:
        os.makedirs(dest)
        kernel_folder = folder_kernel_path(folder) if refs else None
        for rel, (label, uri) in refs.items():
            if os.path.normcase(rel) == os.path.normcase(os.path.basename(src)):
                continue  # the model naming itself; the copy written below serves
            try:
                budget -= _copy_inside(folder, rel, os.path.join(dest, rel), kernel_folder, budget)
            except Refused as e:
                raise Refused(f"refused: {label} {uri!r} {e}") from None
            except (OSError, ValueError) as e:
                raise Refused(f"refused: {label} {uri!r} could not be staged ({e})") from None
        with open(staged, "wb") as f:
            f.write(data)  # the exact bytes that were checked
    except (OSError, ValueError) as e:
        raise Refused(f"the model could not be staged ({e})") from None
    return staged
