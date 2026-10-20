import json
import os
import subprocess
import sys

import pytest

from object_healer import blender as b


def make_exe(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


# --- finding Blender ---------------------------------------------------------------------------

def test_blender_env_wins(tmp_path):
    exe = make_exe(tmp_path / "my-blender")
    app = make_exe(tmp_path / "Blender")
    assert b.find_blender({"BLENDER": exe}, which=lambda _: None, app_paths=(app,)) == exe


def test_blender_env_that_is_not_executable_is_an_error_not_a_fallback(tmp_path):
    app = make_exe(tmp_path / "Blender")
    with pytest.raises(b.BlenderNotFound, match="BLENDER is set to"):
        b.find_blender({"BLENDER": str(tmp_path / "missing")}, which=lambda _: "/x", app_paths=(app,))


def test_blender_app_before_path(tmp_path):
    app = make_exe(tmp_path / "Blender")
    assert b.find_blender({}, which=lambda _: "/usr/bin/blender", app_paths=(app,)) == app


def test_blender_on_path(tmp_path):
    assert b.find_blender({}, which=lambda _: "/opt/bin/blender",
                          app_paths=(str(tmp_path / "nope"),)) == "/opt/bin/blender"


def test_blender_missing_says_how_to_fix(tmp_path):
    with pytest.raises(b.BlenderNotFound, match="Install Blender 4.0 or later"):
        b.find_blender({}, which=lambda _: None, app_paths=(str(tmp_path / "nope"),))


def test_heal_script_ships_with_the_package():
    with open(b.heal_script()) as f:
        assert "import bpy" in f.read()


# --- paths -------------------------------------------------------------------------------------

def test_default_output():
    assert b.default_output("/a/scan.v2.PLY") == "/a/scan.v2_healed.PLY"


@pytest.mark.parametrize("name,out,reason", [
    ("missing.stl", "o.stl", "no such file"),
    ("part.step", "o.stl", "unsupported input format .step"),
    ("part.stl", "o.3mf", "unsupported output format .3mf"),
    ("part.stl", "part.stl", "overwrite the input"),
    ("part.stl", "nodir/o.stl", "does not exist"),
])
def test_check_paths_refuses(tmp_path, name, out, reason):
    for existing in ("part.step", "part.stl"):
        (tmp_path / existing).write_text("x")
    assert reason in b.check_paths(str(tmp_path / name), str(tmp_path / out))


def test_check_paths_accepts_a_format_change(tmp_path):
    (tmp_path / "part.obj").write_text("x")
    assert b.check_paths(str(tmp_path / "part.obj"), str(tmp_path / "part.STL")) is None


# --- running Blender ---------------------------------------------------------------------------

class FakeRun:
    def __init__(self, stdout="", stderr="", returncode=0, exc=None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.cmd = None

    def __call__(self, cmd, **kw):
        self.cmd, self.kw = cmd, kw
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def result_line(**r):
    base = {"status": "ok", "input": "/in.stl", "output": "/out.stl",
            "vertices": 8, "faces": 11, "reason": None}
    return b.RESULT_PREFIX + json.dumps({**base, **r})


def test_run_blender_command_and_result():
    run = FakeRun(stdout="Blender 5.1\nOK:/out.stl:8:11\n" + result_line() + "\n")
    r = b.run_blender("/in.stl", "/out.stl", "/bin/blender", timeout=7, run=run, cwd="/work")
    assert (r.ok, r.output, r.vertices, r.faces) == (True, "/out.stl", 8, 11)
    # factory settings (no user add-ons or startup scripts) and auto-run of embedded Python off
    assert run.cmd[:7] == ["/bin/blender", "--background", "--factory-startup", "--disable-autoexec",
                           "--python-exit-code", "1", "--python"]
    assert "--enable-autoexec" not in run.cmd and "-y" not in run.cmd
    assert run.cmd[7] == b.heal_script()
    assert run.cmd[8:] == ["--", "/in.stl", "--output", "/out.stl"]
    assert run.kw["timeout"] == 7 and run.kw["cwd"] == "/work"


def test_run_blender_reports_the_scripts_error():
    run = FakeRun(stdout=result_line(status="error", output=None, vertices=None, faces=None,
                                     reason="no mesh in the file"), returncode=1)
    r = b.run_blender("/in.stl", "/out.stl", "/bin/blender", run=run, cwd="/work")
    assert (r.ok, r.reason) == (False, "no mesh in the file")


def test_run_blender_without_a_result_line():
    run = FakeRun(stdout="noise\n", stderr="Error: Python script failed\n\n", returncode=1)
    r = b.run_blender("/in.stl", "/out.stl", "/bin/blender", run=run, cwd="/work")
    assert not r.ok
    assert "status 1" in r.reason and "Python script failed" in r.reason


@pytest.mark.parametrize("exc,reason", [
    (subprocess.TimeoutExpired(["blender"], 5), "longer than 5 s"),
    (PermissionError(13, "Permission denied"), "could not start Blender"),
])
def test_run_blender_failures(exc, reason):
    r = b.run_blender("/in.stl", "/out.stl", "/bin/blender", timeout=5, run=FakeRun(exc=exc),
                      cwd="/work")
    assert not r.ok and reason in r.reason


def test_heal_file_refuses_before_starting_blender(tmp_path):
    run = FakeRun()
    r = b.heal_file(str(tmp_path / "missing.stl"), blender="/bin/blender", run=run)
    assert not r.ok and r.reason == "no such file" and run.cmd is None


def test_heal_file_default_output_is_absolute(tmp_path, monkeypatch):
    (tmp_path / "part.stl").write_text("x")
    monkeypatch.chdir(tmp_path)
    run = FakeRun(stdout=result_line())
    b.heal_file("part.stl", blender="/bin/blender", run=run)
    assert run.cmd[-3:] == [str(tmp_path / "part.stl"), "--output", str(tmp_path / "part_healed.stl")]
    # Blender runs from a folder object-healer made, not from wherever the command was typed
    assert os.path.basename(run.kw["cwd"]).startswith("object-healer-")
    assert not os.path.exists(run.kw["cwd"])


@pytest.mark.parametrize("name,out", [("scene.blend", None), ("part.stl", "out.blend"),
                                      ("scene.BLEND1", None)])
def test_blend_files_are_refused(tmp_path, name, out):
    (tmp_path / name).write_text("x")
    run = FakeRun()
    r = b.heal_file(str(tmp_path / name), out and str(tmp_path / out), blender="/bin/blender", run=run)
    assert not r.ok and "embedded Python" in r.reason
    assert run.cmd is None


class SeqRun:
    """Answers each subprocess call in turn and records the commands."""

    def __init__(self, *answers):
        self.answers, self.cmds = list(answers), []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        self.cwds = getattr(self, "cwds", []) + [kw.get("cwd")]
        out, err, code = self.answers.pop(0)
        if cmd[1:3] == ["-m", "object_healer.floaters"] and code == 0:
            open(cmd[-1], "w").close()
        return subprocess.CompletedProcess(cmd, code, out, err)


def test_heal_file_strip_floaters_feeds_blender_the_stripped_copy(tmp_path):
    src = tmp_path / "scan.ply"
    src.write_text("x")
    run = SeqRun(("Warning: noise\n" + json.dumps({"removed": 3}) + "\n", "", 0),
                 (result_line(), "", 0))
    r = b.heal_file(str(src), str(tmp_path / "scan.stl"), blender="/bin/blender",
                    strip_floaters=True, run=run)
    strip_cmd, blender_cmd = run.cmds
    stripped = strip_cmd[-1]
    assert strip_cmd[:5] == [sys.executable, "-I", "-m", "object_healer.floaters", str(src)]
    assert run.cwds[0] == run.cwds[1] == os.path.dirname(stripped)
    assert stripped.endswith("stripped.ply")
    assert blender_cmd[-3:] == [stripped, "--output", str(tmp_path / "scan.stl")]
    assert (r.ok, r.input, r.floaters_removed) == (True, str(src), 3)
    assert not os.path.exists(stripped)


@pytest.mark.parametrize("answer,reason", [
    ((json.dumps({"error": "the file has no faces"}) + "\n", "", 1), "the file has no faces"),
    (("", "Traceback...\nSegmentation fault\n", -11), "exited with status -11: Segmentation fault"),
])
def test_heal_file_strip_failure_is_reported_and_blender_never_runs(tmp_path, answer, reason):
    (tmp_path / "scan.ply").write_text("x")
    run = SeqRun(answer)
    r = b.heal_file(str(tmp_path / "scan.ply"), blender="/bin/blender", strip_floaters=True, run=run)
    assert not r.ok and r.reason == "floater removal failed: " + reason
    assert len(run.cmds) == 1


def test_strip_timeout():
    with pytest.raises(RuntimeError, match="longer than 4 s"):
        b.strip_in_subprocess("/a.ply", "/b.ply", 4, FakeRun(exc=subprocess.TimeoutExpired("x", 4)),
                              cwd="/work")


def plant_fake_package(folder):
    """An object_healer/ package that leaves a marker file if anything imports it."""
    pkg = folder / "object_healer"
    pkg.mkdir()
    marker = folder / "PLANTED-PACKAGE-RAN"
    code = f"open({str(marker)!r}, 'w').close()\n"
    (pkg / "__init__.py").write_text(code)
    (pkg / "floaters.py").write_text(code + "print('{\"removed\": 999}')\n")
    return marker


def test_planted_package_in_the_working_directory_is_not_imported(tmp_path, monkeypatch):
    # the real subprocess, run from (and launched in) a folder holding a fake object_healer/
    marker = plant_fake_package(tmp_path)
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "x.ply"
    src.write_text("not a mesh")
    try:
        removed = b.strip_in_subprocess(str(src), str(tmp_path / "y.ply"), cwd=str(tmp_path))
    except RuntimeError:
        removed = None  # the installed module ran and failed on the junk (or lacks PyMeshLab)
    assert removed != 999
    assert not marker.exists()


def test_heal_file_never_imports_a_planted_package(tmp_path, monkeypatch):
    marker = plant_fake_package(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scan.ply").write_text("not a mesh")
    run_blender_calls = []
    monkeypatch.setattr(b, "run_blender", lambda *a, **kw: run_blender_calls.append(a))
    r = b.heal_file("scan.ply", blender="/bin/blender", strip_floaters=True)
    assert not marker.exists()
    assert not r.ok and r.reason.startswith("floater removal failed")
    assert run_blender_calls == []
