import json
import os
import struct
import zlib

import pytest

from object_healer import cli, gltf
from object_healer import blender as b


def write_gltf(folder, buffers=(), images=(), name="model.gltf"):
    doc = {"asset": {"version": "2.0"},
           "buffers": [{"byteLength": 4, "uri": u} for u in buffers],
           "images": [{"uri": u} for u in images]}
    path = folder / name
    path.write_text(json.dumps(doc))
    return path


def write_glb(folder, doc, name="model.glb"):
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    data = b"glTF" + struct.pack("<II", 2, 20 + len(body)) + struct.pack("<II", len(body), 0x4E4F534A) + body
    path = folder / name
    path.write_bytes(data)
    return path


@pytest.fixture
def model_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "mesh.bin").write_bytes(b"\0" * 4)
    (d / "tex").mkdir()
    (d / "tex" / "skin.png").write_bytes(b"\x89PNG")
    (tmp_path / "secret.bin").write_bytes(b"secret")
    return d


def refused(path, tmp_path):
    with pytest.raises(gltf.Refused) as e:
        gltf.stage(str(path), str(tmp_path / "staged"))
    assert not (tmp_path / "staged" / path.name).exists()  # Blender never gets a model to open
    return str(e.value)


# --- which URIs pass, matched to Blender's importer -------------------------------------------

@pytest.mark.parametrize("uri,rel", [
    ("mesh.bin", "mesh.bin"),
    ("./mesh.bin", "mesh.bin"),
    ("tex/../mesh.bin", "mesh.bin"),
    ("tex\\skin.png", "tex/skin.png"),  # Blender turns backslashes into slashes
    ("tex/sk%69n.png", "tex/skin.png"),  # and percent-decodes
    ("data:application/octet-stream;base64,AAAAAA==", None),
    ("data:;base64,AAAA", None),
    ("data:/../../x;base64,AAAA", None),  # Blender decodes this as data, so it reads no file
    ("", None),  # Blender treats an empty buffer URI as the GLB's own buffer
])
def test_allowed_references(model_dir, uri, rel):
    assert gltf.reference(uri, str(model_dir)) == rel


@pytest.mark.parametrize("uri,why", [
    ("/etc/hosts", "absolute path"),
    ("../secret.bin", "outside the model's folder"),
    ("tex/../../secret.bin", "outside the model's folder"),
    ("%2E%2E/secret.bin", "outside the model's folder"),
    ("..\\secret.bin", "outside the model's folder"),
    ("tex\\..\\..\\secret.bin", "outside the model's folder"),
    ("..", "outside the model's folder"),
    (".", "outside the model's folder"),
    ("data:/../secret.bin", "without ;base64,"),
    ("data:../../secret.bin", "without ;base64,"),
    ("data:text/plain,hello", "without ;base64,"),
    ("data:application/octet-stream;base64", "without ;base64,"),
    ("data:,;base64,AAAA", "without ;base64,"),  # the comma before ;base64, isn't the media type's
    ("file:///etc/hosts", "URI scheme"),
    ("http://example.com/mesh.bin", "URI scheme"),
    ("https://example.com/mesh.bin", "URI scheme"),
    ("ftp://example.com/mesh.bin", "URI scheme"),
    ("smb://server/share/mesh.bin", "URI scheme"),
    (5, "not a string"),
    ("mesh%00.bin", "NUL character"),
    ("%00", "NUL character"),
])
def test_refused_buffer_references(model_dir, tmp_path, uri, why):
    msg = refused(write_gltf(model_dir, buffers=["mesh.bin", uri]), tmp_path)
    assert msg.startswith("refused: buffers[1].uri") and why in msg


def test_image_references_are_checked_too(model_dir, tmp_path):
    msg = refused(write_gltf(model_dir, images=["tex/skin.png", "/etc/hosts"]), tmp_path)
    assert msg.startswith("refused: images[1].uri") and "absolute path" in msg


def test_symlink_out_of_the_folder_is_refused(model_dir, tmp_path):
    os.symlink(tmp_path / "secret.bin", model_dir / "link.bin")
    assert "through a symlink" in refused(write_gltf(model_dir, buffers=["link.bin"]), tmp_path)


def test_symlinked_folder_out_of_the_folder_is_refused(model_dir, tmp_path):
    os.symlink(tmp_path, model_dir / "up")
    assert "through a symlink" in refused(write_gltf(model_dir, buffers=["up/secret.bin"]), tmp_path)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"buffers": {"uri": "/etc/hosts"}}'])
def test_unreadable_gltf_is_refused(model_dir, tmp_path, content):
    path = model_dir / "bad.gltf"
    path.write_text(content)
    assert refused(path, tmp_path).startswith("not a readable glTF file")


def test_glb_json_chunk_is_checked(model_dir, tmp_path):
    ok = write_glb(model_dir, {"asset": {"version": "2.0"}, "buffers": [{"byteLength": 4}]})
    assert os.path.isfile(gltf.stage(str(ok), str(tmp_path / "s1")))
    bad = write_glb(model_dir, {"asset": {"version": "2.0"},
                                "buffers": [{"byteLength": 4}, {"byteLength": 6, "uri": "../secret.bin"}]},
                    name="bad.glb")
    assert "buffers[1].uri" in refused(bad, tmp_path)
    junk = model_dir / "junk.glb"
    junk.write_bytes(b"not a glb at all, just bytes")
    assert refused(junk, tmp_path).startswith("not a readable glTF file")


# --- staging: Blender only ever sees private copies --------------------------------------------

def test_stage_copies_the_model_and_only_its_references(model_dir, tmp_path):
    (model_dir / "unrelated.txt").write_text("not referenced")
    os.symlink(model_dir / "mesh.bin", model_dir / "alias.bin")  # in-folder symlink: allowed
    src = write_gltf(model_dir, buffers=["mesh.bin", "alias.bin", "data:;base64,AAAA"],
                     images=["tex\\skin.png"])
    staged = gltf.stage(str(src), str(tmp_path / "staged"))
    assert staged == str(tmp_path / "staged" / "model.gltf")
    files = sorted(str(p.relative_to(tmp_path / "staged")) for p in (tmp_path / "staged").rglob("*")
                   if p.is_file())
    assert files == ["alias.bin", "mesh.bin", "model.gltf", "tex/skin.png"]
    assert not any(p.is_symlink() for p in (tmp_path / "staged").rglob("*"))
    assert (tmp_path / "staged" / "model.gltf").read_bytes() == src.read_bytes()


def test_missing_reference_is_refused(model_dir, tmp_path):
    assert "does not exist" in refused(write_gltf(model_dir, buffers=["gone.bin"]), tmp_path)


def test_fifo_is_refused_without_hanging(model_dir, tmp_path):
    os.mkfifo(model_dir / "pipe.bin")
    assert "not a regular file" in refused(write_gltf(model_dir, buffers=["pipe.bin"]), tmp_path)


def test_folder_reference_is_refused(model_dir, tmp_path):
    assert "not a regular file" in refused(write_gltf(model_dir, buffers=["tex"]), tmp_path)


@pytest.fixture
def race(monkeypatch):
    """Run before() just ahead of object-healer's open of mesh.bin and after() just behind it:
    the window an attacker has between the containment check and the read."""
    def install(before=None, after=None):
        real_open = os.open

        def racing_open(path, flags, *a, **kw):
            hit = str(path).endswith("mesh.bin")
            if hit and before:
                before(path)
            fd = real_open(path, flags, *a, **kw)
            if hit and after:
                after(path)
            return fd
        monkeypatch.setattr(gltf.os, "open", racing_open)
    return install


def test_swap_to_a_symlink_out_before_the_read_is_caught(model_dir, tmp_path, race):
    src = write_gltf(model_dir, buffers=["mesh.bin"])

    def to_outside(path):
        os.remove(path)
        os.symlink(tmp_path / "secret.bin", path)
    race(before=to_outside)
    assert "changed or moved outside" in refused(src, tmp_path)


def test_swap_out_and_back_around_the_read_is_caught(model_dir, tmp_path, race):
    # the descriptor holds the outside file even though the name looks innocent again
    src = write_gltf(model_dir, buffers=["mesh.bin"])
    saved = tmp_path / "saved.bin"

    def to_outside(path):
        os.replace(path, saved)
        os.symlink(tmp_path / "secret.bin", path)

    def back(path):
        os.remove(path)
        os.replace(saved, path)
    race(before=to_outside, after=back)
    assert "changed or moved outside" in refused(src, tmp_path)


def test_swap_to_another_file_after_the_open_is_caught(model_dir, tmp_path, race):
    src = write_gltf(model_dir, buffers=["mesh.bin"])
    (model_dir / "other.bin").write_bytes(b"other")
    race(after=lambda path: os.replace(model_dir / "other.bin", path))
    assert "changed or moved outside" in refused(src, tmp_path)


# --- through heal_file and the command ----------------------------------------------------------

class NoRun:
    def __call__(self, *a, **kw):
        raise AssertionError("no subprocess may start for a refused file")


def test_heal_file_refuses_before_any_subprocess(model_dir):
    path = write_gltf(model_dir, buffers=["../secret.bin"])
    for strip in (False, True):
        r = b.heal_file(str(path), blender="/bin/blender", strip_floaters=strip, run=NoRun())
        assert not r.ok and "outside the model's folder" in r.reason


def test_heal_file_gives_blender_the_staged_copy(model_dir):
    src = write_gltf(model_dir, buffers=["mesh.bin"])
    seen = {}

    def fake_run(cmd, **kw):
        staged = cmd[cmd.index("--") + 1]
        seen["staged"], seen["cwd"] = staged, kw["cwd"]
        seen["files"] = sorted(os.listdir(os.path.dirname(staged)))
        import subprocess
        line = b.RESULT_PREFIX + json.dumps({"status": "ok", "input": staged, "output": "/o.stl",
                                             "vertices": 8, "faces": 12, "reason": None})
        return subprocess.CompletedProcess(cmd, 0, line + "\n", "")

    r = b.heal_file(str(src), str(model_dir / "o.stl"), blender="/bin/blender", run=fake_run)
    assert r.ok and r.input == str(src)
    assert seen["staged"] == os.path.join(seen["cwd"], "model", "model.gltf")
    assert seen["files"] == ["mesh.bin", "model.gltf"]
    assert not os.path.exists(seen["cwd"])  # the private copy is gone afterwards


def test_cli_exit_status_is_one_for_a_refused_gltf(model_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_blender", lambda env=None: "/bin/blender")
    path = write_gltf(model_dir, buffers=["file:///etc/hosts"])
    assert cli.main(["heal", str(path)]) == 1
    err = capsys.readouterr().err
    assert "failed" in err and "URI scheme" in err


# --- end to end through Blender ----------------------------------------------------------------

def test_a_good_gltf_with_a_sibling_bin_still_heals(blender, broken_cube, tmp_path):
    trimesh = pytest.importorskip("trimesh")
    first = b.heal_file(str(broken_cube), str(tmp_path / "cube.gltf"), blender=blender)
    assert first.ok, first.reason
    doc = json.loads((tmp_path / "cube.gltf").read_text())
    assert [x.get("uri") for x in doc["buffers"]] == ["cube.bin"]  # Blender wrote a sibling .bin
    again = b.heal_file(str(tmp_path / "cube.gltf"), str(tmp_path / "again.stl"), blender=blender)
    assert again.ok, again.reason
    mesh = trimesh.load(str(tmp_path / "again.stl"), force="mesh")
    mesh.merge_vertices()
    assert mesh.is_watertight


def write_png(path, rgb):
    raw = b"".join(b"\0" + bytes(rgb) * 8 for _ in range(8))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_an_obj_mtl_cannot_pull_an_outside_image_into_glb_output(blender, tmp_path):
    # Without the fix this exact setup put the private PNG inside out.glb.
    (tmp_path / "model").mkdir()
    (tmp_path / "private").mkdir()
    secret = tmp_path / "private" / "secret.png"
    write_png(secret, (12, 34, 56))
    (tmp_path / "model" / "evil.mtl").write_text(f"newmtl m\nKd 1 1 1\nmap_Kd {secret}\n")
    corners = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    lines = ["mtllib evil.mtl"] + ["v %d %d %d" % c for c in corners]
    lines += ["vt 0 0", "vt 1 0", "vt 1 1", "vt 0 1", "usemtl m"]
    lines += [f"f {a}/1 {b_}/2 {c}/3 {d}/4" for a, b_, c, d in
              [(1, 2, 3, 4), (5, 8, 7, 6), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 8, 4)]]
    (tmp_path / "model" / "cube.obj").write_text("\n".join(lines) + "\n")
    out = tmp_path / "out.glb"
    r = b.heal_file(str(tmp_path / "model" / "cube.obj"), str(out), blender=blender)
    assert r.ok, r.reason
    data = out.read_bytes()
    doc = json.loads(data[20:20 + struct.unpack_from("<I", data, 12)[0]])
    assert not doc.get("images") and not doc.get("materials")
    assert b"\x89PNG" not in data


def test_nul_in_a_glb_uri_is_refused(model_dir, tmp_path):
    bad = write_glb(model_dir, {"asset": {"version": "2.0"},
                                "images": [{"uri": "skin%00.png"}]}, name="nul.glb")
    assert "images[0].uri" in refused(bad, tmp_path) and "NUL" in refused(bad, tmp_path)


def test_any_valueerror_while_staging_becomes_a_refusal(model_dir, tmp_path, monkeypatch):
    def bad_path(*a):
        raise ValueError("embedded null byte")
    monkeypatch.setattr(gltf, "_copy_inside", bad_path)
    msg = refused(write_gltf(model_dir, buffers=["mesh.bin"]), tmp_path)
    assert "is not a usable path (embedded null byte)" in msg


def test_a_batch_keeps_going_past_a_bad_file(model_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_blender", lambda env=None: "/bin/blender")
    good = []

    def fake_heal(src, dst, **kw):
        if src.endswith("boom.stl"):
            raise RuntimeError("something nobody planned for")
        if src.endswith(".gltf"):
            return b.heal_file(src, dst, run=NoRun(), **kw)
        good.append(src)
        return b.HealResult(True, src, src + ".out", 8, 12)

    monkeypatch.setattr(cli, "heal_file", fake_heal)
    evil = write_gltf(model_dir, buffers=["mesh%00.bin"], name="evil.gltf")
    assert cli.main(["heal", "a.stl", str(evil), "boom.stl", "c.stl"]) == 1
    out, err = capsys.readouterr()
    assert good == ["a.stl", "c.stl"]
    assert "NUL character" in err and "unexpected error: RuntimeError" in err
