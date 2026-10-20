"""Stage a glTF or GLB, and only the files it may reference, into a private folder for Blender.

A .gltf (and the JSON chunk of a .glb) names its buffers and images by URI, and Blender's
importer reads whatever they resolve to. A crafted model could otherwise pull a local file's
bytes into the healed output. So before Blender sees anything:

- every buffers[].uri and images[].uri must be a base64 data: URI or a relative path that stays
  inside the model's own folder, judged with Blender's own rules (below), both on the path as
  written and after symlinks are followed;
- the model and each file it references are copied into a private folder, every referenced file
  read through a descriptor that is checked to still be the in-folder file, and Blender is given
  only the copy, so nothing can be swapped between the check and the import.

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

GLB_MAGIC = b"glTF"
GLB_JSON_CHUNK = 0x4E4F534A
DATA_URI = re.compile(r"data:[^,]*;base64,")


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


def reference(uri, folder):
    """The path, relative to folder, that Blender would read for uri; None for embedded data.
    Raises Refused naming the problem."""
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
    real_folder = os.path.realpath(folder)
    if not _inside(folder, lexical) or rel in (os.curdir, os.pardir):
        raise Refused("points outside the model's folder")
    if not _inside(real_folder, os.path.realpath(lexical)):
        raise Refused("points outside the model's folder (through a symlink)")
    return rel


def _copy_inside(folder, rel, dest):
    """Copy folder/rel to dest through one descriptor, checked to be a regular file that is still
    the in-folder file after symlinks are followed."""
    path = os.path.join(folder, rel)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise Refused("names a file that does not exist") from None
    except OSError as e:
        raise Refused(f"can't be opened ({e.strerror})") from None
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        raise Refused("is not a regular file")
    with os.fdopen(fd, "rb") as f:
        real = os.path.realpath(path)
        try:
            now = os.stat(real)
        except OSError:
            now = None
        if (not _inside(os.path.realpath(folder), real) or now is None
                or (now.st_dev, now.st_ino) != (opened.st_dev, opened.st_ino)):
            raise Refused("changed or moved outside the model's folder while it was being read")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            out = open(dest, "xb")
        except FileExistsError:
            return  # the same file under another case on a case-insensitive disk
        with out:
            while chunk := f.read(1 << 20):
                out.write(chunk)


def stage(src, dest):
    """Copy the model at src and every file it references into the new folder dest, keeping the
    relative layout; return the copy of the model. Raises Refused with a reason for the user."""
    glb = src.lower().endswith(".glb")
    folder = os.path.dirname(os.path.abspath(src))
    with open(src, "rb") as f:
        data = f.read()
    try:
        doc = _json_from(data, glb)
        uris = list(_uris(doc))
    except (ValueError, UnicodeDecodeError) as e:
        raise Refused(f"not a readable glTF file ({e})") from None

    refs = {}
    for label, uri in uris:
        try:
            rel = reference(uri, folder)
        except Refused as e:
            raise Refused(f"refused: {label} {uri!r} {e}") from None
        except ValueError as e:  # a path os functions reject outright
            raise Refused(f"refused: {label} {uri!r} is not a usable path ({e})") from None
        if rel is not None:
            refs.setdefault(rel, (label, uri))

    os.makedirs(dest)
    staged = os.path.join(dest, os.path.basename(src))
    for rel, (label, uri) in refs.items():
        if os.path.normcase(rel) == os.path.normcase(os.path.basename(src)):
            continue  # the model naming itself; the copy written below serves
        try:
            _copy_inside(folder, rel, os.path.join(dest, rel))
        except Refused as e:
            raise Refused(f"refused: {label} {uri!r} {e}") from None
        except ValueError as e:
            raise Refused(f"refused: {label} {uri!r} is not a usable path ({e})") from None
    with open(staged, "wb") as f:
        f.write(data)  # the exact bytes that were checked
    return staged
