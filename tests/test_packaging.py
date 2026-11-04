from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONSUMER = (
    "import collide_logging\n"
    "import collide_logging.contract\n"
    "import collide_logging.outage\n"
    "import collide_logging.testing\n"
    "\n"
    'collide_logging.get_logger("x").info("a.b")\n'
)


def _build(out_dir: Path) -> tuple[Path, Path]:
    subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    (wheel,) = out_dir.glob("*.whl")
    (sdist,) = out_dir.glob("*.tar.gz")
    return wheel, sdist


def test_built_distributions_ship_py_typed(tmp_path: Path) -> None:
    wheel, sdist = _build(tmp_path)
    assert "collide_logging/py.typed" in zipfile.ZipFile(wheel).namelist()
    with tarfile.open(sdist) as archive:
        assert any(name.endswith("src/collide_logging/py.typed") for name in archive.getnames())


def test_strict_consumer_sees_no_import_untyped(tmp_path: Path) -> None:
    wheel, _ = _build(tmp_path / "dist")
    (tmp_path / "consumer.py").write_text(CONSUMER)
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--with",
            str(wheel),
            "--with",
            "mypy==2.3.1",
            "mypy",
            "--strict",
            "--cache-dir",
            str(tmp_path / "cache"),
            "consumer.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "import-untyped" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
