import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_hook", ROOT / "scripts/install_hook.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def fake_bun(tmp_path, monkeypatch):
    directory = tmp_path / "fake bin"
    directory.mkdir()
    log = tmp_path / "bun-calls.jsonl"
    binary = directory / "bun"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert pathlib.Path('package.json').is_file()\n"
        "assert pathlib.Path('tsconfig.json').is_file()\n"
        "assert pathlib.Path('bun.lock').is_file()\n"
        f"with open({str(log)!r}, 'a') as handle:\n"
        "    handle.write(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'node_env': os.environ.get('NODE_ENV')}) + '\\n')\n"
        "if os.environ.get('HARNESS_ROUTER_TEST_BUN_FAIL') == sys.argv[1]:\n"
        "    sys.exit(1)\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ.get("PATH", ""))
    return log


def test_install_preserves_other_extensions_settings_and_is_idempotent(tmp_path):
    config = tmp_path / ".omp/config.yml"
    config.parent.mkdir()
    config.write_text("# keep formatting\nextensions: [./my-extension.ts]\n")
    other = tmp_path / ".omp/extensions/other.ts"
    other.parent.mkdir()
    other.write_text("export default function () {}\n")
    ignore = tmp_path / ".gitignore"
    ignore.write_text("existing/\n")
    assert len(installer.install(tmp_path, ["ohmypi"], ROOT, "main")) == 6
    for relative in (
        ".omp/extensions/harness-router.ts",
        ".omp/hooks/pre_tool_use.py",
        ".omp/tsconfig.json",
        ".omp/package.json",
        ".omp/bun.lock",
    ):
        assert (tmp_path / relative).read_bytes() == (ROOT / relative).read_bytes()
    assert config.read_text() == "# keep formatting\nextensions: [./my-extension.ts]\n"
    assert other.read_text() == "export default function () {}\n"
    assert (tmp_path / ".gitignore.harness-router.bak").read_text() == "existing/\n"
    assert ".omp/harness-router/" in ignore.read_text().splitlines()
    assert ".omp/node_modules/" in ignore.read_text().splitlines()
    assert installer.install(tmp_path, ["ohmypi"], ROOT, "main") == []
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()


@pytest.mark.parametrize(
    "relative",
    [
        ".omp/extensions/harness-router.ts",
        ".omp/tsconfig.json",
        ".omp/package.json",
        ".omp/bun.lock",
    ],
)
def test_modified_assets_are_backed_up(tmp_path, relative):
    installer.install(tmp_path, ["ohmypi"], ROOT, "main")
    target = tmp_path / relative
    target.write_text("customized asset\n")
    installer.install(tmp_path, ["ohmypi"], ROOT, "main")
    assert target.with_name(target.name + ".harness-router.bak").read_text() == "customized asset\n"
    assert target.read_bytes() == (ROOT / relative).read_bytes()


@pytest.mark.parametrize(
    "blocked",
    [
        ".omp",
        ".omp/extensions",
        ".omp/hooks",
        ".omp/tsconfig.json",
        ".omp/package.json",
        ".omp/bun.lock",
    ],
)
def test_symlinks_cannot_modify_other_directories(tmp_path, blocked):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = project / blocked
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.install(project, ["ohmypi"], ROOT, "main")
    assert not list(outside.iterdir())
    assert not (project / ".gitignore").exists()


@pytest.mark.parametrize("missing", [".omp/hooks/pre_tool_use.py", ".omp/bun.lock"])
def test_asset_failure_leaves_no_partial_installation(tmp_path, monkeypatch, missing):
    def asset(path, source, ref):
        if path == missing:
            raise OSError("download failed")
        return (ROOT / path).read_bytes()

    monkeypatch.setattr(installer, "asset", asset)
    with pytest.raises(OSError, match="download failed"):
        installer.install(tmp_path, ["ohmypi"], None, "test-ref")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("invalid", [".omp/tsconfig.json", ".omp/package.json"])
def test_invalid_development_asset_leaves_project_unchanged(tmp_path, monkeypatch, invalid):
    def asset(path, source, ref):
        return b"invalid json" if path == invalid else (ROOT / path).read_bytes()

    monkeypatch.setattr(installer, "asset", asset)
    with pytest.raises(ValueError, match="not valid JSON"):
        installer.install(tmp_path, ["ohmypi"], None, "test-ref")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("ohmypi", [".omp"]),
        ("codex", [".codex"]),
        ("claude", [".claude"]),
        ("both", [".codex", ".claude"]),
        ("all", [".codex", ".claude", ".omp"]),
    ],
)
def test_stdin_cli_provider_selection_remains_compatible(
    tmp_path, provider, expected, fake_bun, monkeypatch
):
    project = tmp_path / "project with spaces"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    monkeypatch.setenv("NODE_ENV", "production")
    run = subprocess.run(
        [
            sys.executable,
            "-",
            "--provider",
            provider,
            "--source",
            str(ROOT),
            "--project",
            str(project),
        ],
        input=(ROOT / "scripts/install_hook.py").read_text(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert "Installed" in run.stdout
    for directory in (".codex", ".claude", ".omp"):
        assert (project / directory).exists() == (directory in expected)
    if ".omp" in expected:
        assert (project / ".omp/tsconfig.json").is_file()
        assert (project / ".omp/package.json").is_file()
        assert (project / ".omp/bun.lock").is_file()
        assert "TypeScript dependencies are installed and typecheck passed" in run.stdout
        calls = [json.loads(line) for line in fake_bun.read_text().splitlines()]
        assert [call["args"] for call in calls] == [
            ["install", "--frozen-lockfile", "--ignore-scripts"],
            ["run", "typecheck"],
        ]
        assert all(call["cwd"] == str(project / ".omp") for call in calls)
        assert all(call["node_env"] == "development" for call in calls)
        assert os.environ["NODE_ENV"] == "production"
    else:
        assert not fake_bun.exists()


def test_cli_requires_bun_before_writing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    assert (
        installer.main(
            [
                "--provider",
                "ohmypi",
                "--project",
                str(tmp_path),
                "--source",
                str(ROOT),
            ]
        )
        == 1
    )
    assert "Bun is required" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_cli_can_explicitly_skip_dependency_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    assert (
        installer.main(
            [
                "--provider",
                "ohmypi",
                "--project",
                str(tmp_path),
                "--source",
                str(ROOT),
                "--skip-ohmypi-deps",
            ]
        )
        == 0
    )
    assert (tmp_path / ".omp/tsconfig.json").exists()
    assert "setup was skipped" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["install", "run"])
def test_dependency_failure_is_reported_and_can_be_retried(
    tmp_path, monkeypatch, capsys, fake_bun, failure
):
    project = tmp_path / "project"
    project.mkdir()
    arguments = ["--provider", "ohmypi", "--project", str(project), "--source", str(ROOT)]
    monkeypatch.setenv("HARNESS_ROUTER_TEST_BUN_FAIL", failure)
    assert installer.main(arguments) == 1
    output = capsys.readouterr()
    assert "dependency setup failed" in output.err
    assert "rerun the installer" in output.err
    assert "typecheck passed" not in output.out
    assert (project / ".omp/tsconfig.json").is_file()
    assert len(fake_bun.read_text().splitlines()) == (1 if failure == "install" else 2)
    monkeypatch.delenv("HARNESS_ROUTER_TEST_BUN_FAIL")
    assert installer.main(arguments) == 0
    assert "typecheck passed" in capsys.readouterr().out
