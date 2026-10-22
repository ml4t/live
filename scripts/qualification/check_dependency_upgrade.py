"""Reject upgrades that retain vulnerable SDK or certificate dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from scripts.qualification.audit_dependencies import osv_advisories

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], environment: dict[str, str] | None = None) -> None:
    print(f"$ {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, env=environment, check=True)


def check_upgrade() -> tuple[dict[str, str], dict[str, list[str]]]:
    with tempfile.TemporaryDirectory(prefix="ml4t-live-upgrade-") as directory:
        root = Path(directory)
        venv = root / "venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run(["uv", "venv", "--python", "3.12", str(venv)])
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "ml4t-live==0.1.2",
                "ccxt==4.5.74",
            ]
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("SETUPTOOLS_SCM_PRETEND_VERSION", None)
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(root / "dist"),
                "--build-constraints",
                "build-constraints.txt",
            ],
            environment,
        )
        wheel = next((root / "dist").glob("*.whl"))
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--upgrade-package",
                "ml4t-live",
                str(wheel),
            ],
            environment,
        )
        versions: dict[str, str] = json.loads(
            subprocess.check_output(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json,importlib.metadata as m; "
                    "print(json.dumps({n:m.version(n) for n in "
                    "('ml4t-live','ccxt','urllib3','multidict','certifi')}))",
                ],
                cwd=root,
                env=environment,
                text=True,
            )
        )
        advisories = osv_advisories(
            [(name, version) for name, version in versions.items() if name != "ml4t-live"]
        )
        return versions, advisories


def main() -> int:
    versions, advisories = check_upgrade()
    print(json.dumps({"versions": versions, "advisories": advisories}, indent=2, sort_keys=True))
    failed = any(advisories.values())
    print(f"existing-environment upgrade: {'FAIL' if failed else 'PASS'}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
