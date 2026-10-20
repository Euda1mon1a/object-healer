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
    src = write_gltf(model_dir, buffers=["mesh.bin", "data:;base64,AAAA"], images=["tex\\skin.png"])
    staged = gltf.stage(str(src), str(tmp_path / "staged"))
    assert staged == str(tmp_path / "staged" / "model.gltf")
    files = sorted(str(p.relative_to(tmp_path / "staged")) for p in (tmp_path / "staged").rglob("*")
                   if p.is_file())
    assert files == ["mesh.bin", "model.gltf", "tex/skin.png"]
    assert (tmp_path / "staged" / "model.gltf").read_bytes() == src.read_bytes()


@pytest.mark.parametrize("link,target", [("alias.bin", "mesh.bin"), ("alias", "tex")])
def test_symlinks_inside_the_folder_are_refused_too(model_dir, tmp_path, link, target):
    # Nothing is opened through a symlink, so even a harmless in-folder one is refused.
    os.symlink(model_dir / target, model_dir / link)
    uri = link if link.endswith(".bin") else link + "/skin.png"
    assert "symlink" in refused(write_gltf(model_dir, buffers=[uri]), tmp_path)


def test_missing_reference_is_refused(model_dir, tmp_path):
    assert "does not exist" in refused(write_gltf(model_dir, buffers=["gone.bin"]), tmp_path)


def test_fifo_is_refused_without_hanging(model_dir, tmp_path):
    os.mkfifo(model_dir / "pipe.bin")
    assert "not a regular file" in refused(write_gltf(model_dir, buffers=["pipe.bin"]), tmp_path)


def test_folder_reference_is_refused(model_dir, tmp_path):
    assert "not a regular file" in refused(write_gltf(model_dir, buffers=["tex"]), tmp_path)


@pytest.fixture
def race(monkeypatch):
    """Run before() just ahead of object-healer's open of the name `at` and after() just behind
    it: the window an attacker has between the containment check and the read."""
    def install(at="mesh.bin", before=None, after=None):
        real_open = os.open

        def racing_open(path, flags, *a, **kw):
            hit = os.path.basename(str(path)) == at
            if hit and before:
                before()
            fd = real_open(path, flags, *a, **kw)
            if hit and after:
                after()
            return fd
        monkeypatch.setattr(gltf.os, "open", racing_open)
    return install


def staged_or_refused(src, tmp_path, rel="mesh.bin"):
    """The bytes staged for rel, or None if staging refused."""
    try:
        gltf.stage(str(src), str(tmp_path / "staged"))
    except gltf.Refused:
        return None
    return (tmp_path / "staged" / rel).read_bytes()


def test_swap_to_a_symlink_out_before_the_read_is_refused(model_dir, tmp_path, race):
    src = write_gltf(model_dir, buffers=["mesh.bin"])

    def to_outside():
        os.remove(model_dir / "mesh.bin")
        os.symlink(tmp_path / "secret.bin", model_dir / "mesh.bin")
    race(before=to_outside)
    assert "symlink" in refused(src, tmp_path)


def test_swap_after_the_open_copies_what_was_opened(model_dir, tmp_path, race):
    src = write_gltf(model_dir, buffers=["mesh.bin"])
    (model_dir / "other.bin").write_bytes(b"other")
    race(after=lambda: os.replace(model_dir / "other.bin", model_dir / "mesh.bin"))
    assert staged_or_refused(src, tmp_path) == b"\0" * 4


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
    assert "could not be staged (embedded null byte)" in msg


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


# --- 0.1.1: where the open file really is, and what staging may cost --------------------------

@pytest.fixture
def two_folders(tmp_path):
    """model/ (sub/mesh.bin = INFOLDER) and outside/ (sub/mesh.bin and mesh.bin = CANARY)."""
    model, outside = tmp_path / "model", tmp_path / "outside"
    for d in (model / "sub", outside / "sub"):
        d.mkdir(parents=True)
    (model / "sub" / "mesh.bin").write_bytes(b"INFOLDER")
    (outside / "sub" / "mesh.bin").write_bytes(b"CANARY")
    (outside / "mesh.bin").write_bytes(b"CANARY")
    return model, outside


def test_sub_folder_swapped_around_the_file_open_stays_inside(tmp_path, two_folders, race):
    # The reviewer's interleaving: sub/ becomes a symlink to outside/ for the file's open and a
    # real folder again after it. sub/ is already open by then, so only in-folder bytes come.
    model, outside = two_folders
    src = write_gltf(model, buffers=["sub/mesh.bin"], name="m.gltf")
    sub, sub_real = model / "sub", model / "sub_real"

    def to_link():
        os.rename(sub, sub_real)
        os.symlink(outside, sub)

    def to_real():
        os.remove(sub)
        os.rename(sub_real, sub)
    race(at="mesh.bin", before=to_link, after=to_real)
    assert staged_or_refused(src, tmp_path, "sub/mesh.bin") == b"INFOLDER"


def test_sub_folder_swapped_before_its_open_is_refused(tmp_path, two_folders, race):
    model, outside = two_folders
    src = write_gltf(model, buffers=["sub/mesh.bin"], name="m.gltf")

    def to_link():
        os.rename(model / "sub", model / "sub_real")
        os.symlink(outside / "sub", model / "sub")
    race(at="sub", before=to_link)
    assert "symlink" in refused(src, tmp_path)


@pytest.mark.parametrize("swap_after_check,expect", [
    (False, None),  # swapped before the name check: the check sees outside/ and refuses
    (True, b"INFOLDER"),  # swapped after it: only the open folder keeps the reads inside
])
def test_model_folder_renamed_between_the_model_read_and_the_references(
        tmp_path, two_folders, monkeypatch, swap_after_check, expect):
    # After the model is read, model/ moves away and its name becomes a symlink to outside/,
    # which has the same layout. Opening references by the folder's name would read CANARY.
    model, outside = two_folders
    src = write_gltf(model, buffers=["sub/mesh.bin"], name="m.gltf")
    real_reference = gltf.reference

    def swap_folder():
        os.rename(model, tmp_path / "model_away")
        os.symlink(outside, model)

    def racing_reference(*a, **kw):
        if not swap_after_check:
            swap_folder()
        rel = real_reference(*a, **kw)
        if swap_after_check:
            swap_folder()
        return rel
    monkeypatch.setattr(gltf, "reference", racing_reference)
    assert staged_or_refused(src, tmp_path, "sub/mesh.bin") == expect


def test_parent_folder_swapped_before_the_folder_open_is_refused(tmp_path, race):
    # a/model is opened through a, which is switched to a symlink to elsewhere just before
    for d in ("a/model", "elsewhere/model"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "a/model/mesh.bin").write_bytes(b"INFOLDER")
    (tmp_path / "elsewhere/model/mesh.bin").write_bytes(b"CANARY")
    src = write_gltf(tmp_path / "a/model", buffers=["mesh.bin"])
    write_gltf(tmp_path / "elsewhere/model", buffers=["mesh.bin"])

    def swap_parent():
        os.rename(tmp_path / "a", tmp_path / "a_away")
        os.symlink(tmp_path / "elsewhere", tmp_path / "a")
    race(at="model", before=swap_parent)
    assert "moved while it was being opened" in refused(src, tmp_path)


def test_kernel_path_matches_the_opened_file(model_dir):
    dfd = os.open(model_dir, os.O_RDONLY)
    fd = os.open("mesh.bin", os.O_RDONLY, dir_fd=dfd)
    try:
        assert gltf.kernel_path(fd) == os.path.join(gltf.kernel_path(dfd), "mesh.bin")
    finally:
        os.close(fd)
        os.close(dfd)


def test_folder_typed_in_another_case_still_stages(tmp_path):
    (tmp_path / "Model").mkdir()
    (tmp_path / "Model" / "mesh.bin").write_bytes(b"\0" * 4)
    write_gltf(tmp_path / "Model", buffers=["mesh.bin"])
    typed = tmp_path / "model" / "model.gltf"
    if not typed.exists():
        pytest.skip("case-sensitive file system")
    assert os.path.isfile(gltf.stage(str(typed), str(tmp_path / "staged")))


def test_folder_typed_in_another_unicode_form_still_stages(tmp_path):
    import unicodedata
    stored = tmp_path / unicodedata.normalize("NFD", "Café")
    stored.mkdir()
    (stored / "mesh.bin").write_bytes(b"\0" * 4)
    write_gltf(stored, buffers=["mesh.bin"])
    typed = os.path.join(str(tmp_path), unicodedata.normalize("NFC", "Café"), "model.gltf")
    if not os.path.exists(typed):
        pytest.skip("normalization-sensitive file system")
    assert os.path.isfile(gltf.stage(typed, str(tmp_path / "staged")))


def test_the_model_itself_is_size_capped_and_not_sparse(model_dir, tmp_path, monkeypatch):
    big = write_gltf(model_dir, buffers=["mesh.bin"], name="big.gltf")
    monkeypatch.setattr(gltf, "MAX_MODEL_BYTES", 10)
    assert "over the" in refused(big, tmp_path / "big")
    monkeypatch.undo()
    hole = model_dir / "hole.glb"
    fd = os.open(hole, os.O_RDWR | os.O_CREAT)  # one byte at 64 MiB; APFS keeps a gap that big a hole
    os.pwrite(fd, b"x", 64 << 20)
    os.close(fd)
    st = os.stat(hole)
    if st.st_blocks * 512 * 4 >= st.st_size:
        pytest.skip("this file system doesn't store sparse files")
    assert "sparse" in refused(hole, tmp_path / "hole")


def test_no_way_to_ask_the_kernel_refuses_gltf(model_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(gltf, "fcntl", None)
    monkeypatch.setattr(gltf, "PROC_FD", str(tmp_path / "no-proc"))
    assert "can't be confirmed" in refused(write_gltf(model_dir, buffers=["mesh.bin"]), tmp_path)
    plain = write_glb(model_dir, {"asset": {"version": "2.0"}, "buffers": [{"byteLength": 4}]})
    assert "can't be confirmed" in refused(plain, tmp_path / "plain")


def test_hard_links_are_refused(model_dir, tmp_path):
    os.link(model_dir / "mesh.bin", model_dir / "twin.bin")
    assert "hard links" in refused(write_gltf(model_dir, buffers=["mesh.bin"]), tmp_path)


def test_size_cap_per_file_and_in_total(model_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(gltf, "MAX_REFERENCED_BYTES", 10)
    (model_dir / "big.bin").write_bytes(b"x" * 11)
    assert "limit" in refused(write_gltf(model_dir, buffers=["big.bin"], name="a.gltf"), tmp_path)
    (model_dir / "six1.bin").write_bytes(b"x" * 6)
    (model_dir / "six2.bin").write_bytes(b"x" * 6)
    msg = refused(write_gltf(model_dir, buffers=["six1.bin", "six2.bin"], name="b.gltf"), tmp_path / "b")
    assert msg.startswith("refused: buffers[1].uri") and "limit" in msg
    ok = write_gltf(model_dir, buffers=["six1.bin", "mesh.bin"], name="c.gltf")
    assert os.path.isfile(gltf.stage(str(ok), str(tmp_path / "c")))


def test_the_real_cap_is_one_gib():
    assert gltf.MAX_REFERENCED_BYTES == 1 << 30


def test_a_file_growing_during_the_copy_stops_at_the_limit():
    import io
    with pytest.raises(gltf.Refused, match="grew past"):
        gltf._copy_bounded(io.BytesIO(b"x" * 20), io.BytesIO(), 10)


def test_sparse_files_are_refused(model_dir, tmp_path):
    hole = model_dir / "hole.bin"
    fd = os.open(hole, os.O_RDWR | os.O_CREAT)  # one byte at 64 MiB; APFS keeps a gap that big a hole
    os.pwrite(fd, b"x", 64 << 20)
    os.close(fd)
    st = os.stat(hole)
    if st.st_blocks * 512 * 4 >= st.st_size:
        pytest.skip("this file system doesn't store sparse files")
    assert "sparse" in refused(write_gltf(model_dir, buffers=["hole.bin"]), tmp_path)
    dense = model_dir / "dense.bin"
    dense.write_bytes(os.urandom(2 << 20))
    assert os.path.isfile(gltf.stage(str(write_gltf(model_dir, buffers=["dense.bin"], name="d.gltf")),
                                     str(tmp_path / "d")))


def test_out_of_space_while_copying_is_a_refusal(model_dir, tmp_path, monkeypatch):
    import errno

    def full(*a):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(gltf, "_copy_bounded", full)
    msg = refused(write_gltf(model_dir, buffers=["mesh.bin"]), tmp_path)
    assert "could not be staged" in msg and "No space left" in msg


def test_unwritable_staging_folder_is_a_refusal(model_dir, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(gltf.Refused, match="could not be staged"):
            gltf.stage(str(write_gltf(model_dir, buffers=["mesh.bin"])), str(locked / "dest"))
    finally:
        locked.chmod(0o700)


def test_unreadable_model_is_a_refusal(model_dir, tmp_path):
    src = write_gltf(model_dir, buffers=["mesh.bin"])
    src.chmod(0o000)
    try:
        with pytest.raises(gltf.Refused, match="could not be read"):
            gltf.stage(str(src), str(tmp_path / "dest"))
    finally:
        src.chmod(0o600)
