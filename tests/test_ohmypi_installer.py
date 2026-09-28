import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_hook", ROOT / "scripts/install_hook.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


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
def test_stdin_cli_provider_selection_remains_compatible(tmp_path, provider, expected):
    project = tmp_path / "project with spaces"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
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
        assert "bun install --frozen-lockfile --ignore-scripts" in run.stdout
