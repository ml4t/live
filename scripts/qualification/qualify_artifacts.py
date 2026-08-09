"""Qualify release artifacts as installed distributions outside the checkout."""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from email.message import Message
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

try:
    from scripts.qualification.check_public_claims import DETERMINISTIC_EXAMPLES
    from scripts.qualification.scan_release_secrets import scan_release
except ModuleNotFoundError:
    from check_public_claims import DETERMINISTIC_EXAMPLES
    from scan_release_secrets import scan_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "artifact-manifest.toml"
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
INSTALLED_SMOKE = Path(__file__).with_name("installed_smoke.py")
SUPPORTED_PYTHONS = ("3.12", "3.13", "3.14")
REJECTED_PYTHON = "3.15"
EXPECTED_URLS = {
    "Homepage": "https://www.ml4trading.io/docs/live/",
    "Documentation": "https://www.ml4trading.io/docs/live/",
    "Repository": "https://github.com/ml4t/live",
    "Issues": "https://github.com/ml4t/live/issues",
    "Changelog": "https://github.com/ml4t/live/releases",
}
EXPECTED_CLASSIFIERS = {
    "Development Status :: 4 - Beta",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.12",
    "Programming Language :: Python :: 3.13",
    "Programming Language :: Python :: 3.14",
    "Typing :: Typed",
}


class QualificationError(RuntimeError):
    """Artifact qualification failed."""


@dataclass(frozen=True)
class ProfileResult:
    artifact: str
    python: str
    passed: bool
    failed_step: str | None = None


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != expected_returncode:
        raise QualificationError(f"command failed ({result.returncode}): {' '.join(command[:4])}")
    return result


def _git_output(*arguments: str) -> str:
    return _run(("git", *arguments), cwd=REPOSITORY_ROOT).stdout.strip()


def _source_date_epoch() -> str:
    return _git_output("show", "-s", "--format=%ct", "HEAD")


def _repository_status() -> str:
    return _git_output("status", "--porcelain=v1", "--untracked-files=all")


def build_distributions(destination: Path, source_date_epoch: str) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    _run(
        (
            "uv",
            "build",
            "--out-dir",
            str(destination),
            "--build-constraints",
            str(BUILD_CONSTRAINTS),
        ),
        cwd=REPOSITORY_ROOT,
        environment={"SOURCE_DATE_EPOCH": source_date_epoch},
    )
    wheels = sorted(destination.glob("*.whl"))
    sdists = sorted(destination.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise QualificationError("build did not produce exactly one wheel and one sdist")
    return wheels[0], sdists[0]


def distribution_pair(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise QualificationError("artifact input must contain exactly one wheel and one sdist")
    return wheels[0], sdists[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_reproducible(first: Iterable[Path], second: Iterable[Path]) -> None:
    first_by_type = {"wheel" if path.suffix == ".whl" else "sdist": path for path in first}
    second_by_type = {"wheel" if path.suffix == ".whl" else "sdist": path for path in second}
    if set(first_by_type) != {"wheel", "sdist"} or set(second_by_type) != {"wheel", "sdist"}:
        raise QualificationError("reproducibility comparison is missing an artifact type")
    for artifact_type in ("wheel", "sdist"):
        left = first_by_type[artifact_type]
        right = second_by_type[artifact_type]
        if left.name != right.name or _sha256(left) != _sha256(right):
            raise QualificationError(f"{artifact_type} build is not byte-for-byte reproducible")


def normalized_wheel_manifest(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        names = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
    return {re.sub(r"^ml4t_live-[^/]+\.dist-info/", "<dist-info>/", name) for name in names}


def normalized_sdist_manifest(path: Path) -> set[str]:
    with tarfile.open(path) as archive:
        names = [member.name for member in archive.getmembers() if member.isfile()]
    roots = {name.partition("/")[0] for name in names}
    if len(roots) != 1 or any("/" not in name for name in names):
        raise QualificationError("sdist must have one versioned root directory")
    return {name.partition("/")[2] for name in names}


def load_expected_manifests(path: Path = MANIFEST_PATH) -> tuple[set[str], set[str]]:
    with path.open("rb") as stream:
        manifest = tomllib.load(stream)
    if manifest.get("schema-version") != 1:
        raise QualificationError("unsupported artifact manifest schema")
    return set(manifest["wheel"]), set(manifest["sdist"])


def _manifest_difference(expected: set[str], actual: set[str]) -> str:
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    return f"missing={missing}; unexpected={unexpected}"


def validate_manifests(wheel: Path, sdist: Path) -> None:
    expected_wheel, expected_sdist = load_expected_manifests()
    actual_wheel = normalized_wheel_manifest(wheel)
    actual_sdist = normalized_sdist_manifest(sdist)
    if actual_wheel != expected_wheel:
        raise QualificationError(
            f"wheel manifest mismatch: {_manifest_difference(expected_wheel, actual_wheel)}"
        )
    if actual_sdist != expected_sdist:
        raise QualificationError(
            f"sdist manifest mismatch: {_manifest_difference(expected_sdist, actual_sdist)}"
        )


def _wheel_member(path: Path, suffix: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        matches = [name for name in archive.namelist() if name.endswith(suffix)]
        if len(matches) != 1:
            raise QualificationError(f"wheel has {len(matches)} entries ending in {suffix}")
        return archive.read(matches[0])


def _sdist_member(path: Path, suffix: str) -> bytes:
    with tarfile.open(path) as archive:
        matches = [member for member in archive.getmembers() if member.name.endswith(suffix)]
        if len(matches) != 1:
            raise QualificationError(f"sdist has {len(matches)} entries ending in {suffix}")
        extracted = archive.extractfile(matches[0])
        if extracted is None:
            raise QualificationError(f"could not read sdist entry ending in {suffix}")
        return extracted.read()


def _metadata(payload: bytes) -> Message:
    return email.message_from_bytes(payload)


def validate_metadata(message: Message, project: dict[str, object]) -> str:
    failures: list[str] = []
    if message["Name"] != "ml4t-live":
        failures.append("Name")
    if SpecifierSet(message["Requires-Python"] or "") != SpecifierSet(">=3.12,<3.15"):
        failures.append("Requires-Python")
    if message["License-Expression"] != "MIT":
        failures.append("License-Expression")

    version_text = message["Version"]
    try:
        version = Version(version_text)
    except Exception as error:
        raise QualificationError("artifact version is not PEP 440 compliant") from error
    if version.release != (0, 1, 0) or version.pre != ("b", 4):
        failures.append("Version")

    classifiers = set(message.get_all("Classifier", []))
    if not EXPECTED_CLASSIFIERS <= classifiers:
        failures.append("Classifier")

    urls: dict[str, str] = {}
    for value in message.get_all("Project-URL", []):
        label, separator, url = value.partition(", ")
        if not separator:
            failures.append("Project-URL")
            continue
        urls[label] = url
    if urls != EXPECTED_URLS:
        failures.append("Project-URL")

    expected_dependencies = {Requirement(value) for value in project["dependencies"]}  # type: ignore[arg-type]
    runtime_dependencies = {
        requirement
        for value in message.get_all("Requires-Dist", [])
        if (requirement := Requirement(value)).marker is None
    }
    if runtime_dependencies != expected_dependencies:
        failures.append("Requires-Dist")
    if failures:
        raise QualificationError(
            f"invalid core metadata fields: {', '.join(sorted(set(failures)))}"
        )
    return version_text


def validate_artifact_metadata(wheel: Path, sdist: Path) -> str:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    wheel_metadata = _metadata(_wheel_member(wheel, ".dist-info/METADATA"))
    sdist_metadata = _metadata(_sdist_member(sdist, "/PKG-INFO"))
    wheel_version = validate_metadata(wheel_metadata, project)
    sdist_version = validate_metadata(sdist_metadata, project)
    if wheel_version != sdist_version:
        raise QualificationError("wheel and sdist versions differ")

    wheel_descriptor = email.message_from_bytes(_wheel_member(wheel, ".dist-info/WHEEL"))
    if wheel_descriptor["Root-Is-Purelib"] != "true" or set(
        wheel_descriptor.get_all("Tag", [])
    ) != {"py3-none-any"}:
        raise QualificationError("wheel is not a portable pure-Python distribution")
    entry_points = _wheel_member(wheel, ".dist-info/entry_points.txt").decode()
    if entry_points != "[console_scripts]\nml4t-live = ml4t.live.cli.main:app\n":
        raise QualificationError("console entry point differs from the public CLI contract")
    return wheel_version


def _profile_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _profile_cli(venv: Path) -> Path:
    return venv / ("Scripts/ml4t-live.exe" if os.name == "nt" else "bin/ml4t-live")


def run_installed_examples(python: Path, root: Path) -> None:
    """Run every credential-free maintained example against an installed wheel."""
    example_root = root / "examples"
    example_root.mkdir(parents=True)
    expected_output = {
        "risk_guard_demo.py": (
            "fresh_data_order: accepted",
            "stale_data_block:",
            "kill_switch_active: True",
        ),
        "shadow_mode_demo.py": ("Starting shadow mode demo", "Finished shadow mode demo"),
        "startup_reconciliation_demo.py": ("Startup reconciliation report:", '"clean": false'),
    }
    environment = {
        "ML4T_EXAMPLE_DURATION_SECONDS": "8",
        "ML4T_EXAMPLE_TICK_SECONDS": "0.001",
    }
    for name in sorted(DETERMINISTIC_EXAMPLES):
        source = REPOSITORY_ROOT / "examples" / name
        destination = example_root / name
        shutil.copy2(source, destination)
        result = _run((str(python), "-I", str(destination)), cwd=root, environment=environment)
        missing = [marker for marker in expected_output[name] if marker not in result.stdout]
        if missing:
            raise QualificationError(f"installed example {name} omitted output markers: {missing}")


def install_profile(artifact: Path, python_version: str, expected_version: str, root: Path) -> None:
    venv = root / "venv"
    _run(("uv", "venv", "--python", python_version, str(venv)), cwd=root)
    python = _profile_python(venv)
    _run(
        (
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--build-constraints",
            str(BUILD_CONSTRAINTS),
            str(artifact),
        ),
        cwd=root,
    )
    _run(
        (
            str(python),
            "-I",
            "-c",
            (
                "import ml4t.live as package; "
                f"assert package.__version__ == {expected_version!r}; "
                "assert package.__file__ is not None"
            ),
        ),
        cwd=root,
    )
    _run((str(_profile_cli(venv)), "--version"), cwd=root)
    _run((str(python), "-I", str(INSTALLED_SMOKE)), cwd=root)
    if artifact.suffix == ".whl" and python_version == SUPPORTED_PYTHONS[0]:
        run_installed_examples(python, root)

    consumer = root / "consumer.py"
    consumer.write_text(
        "from ml4t.live import LiveRiskConfig\n"
        "config: LiveRiskConfig = LiveRiskConfig(max_orders_per_minute=1)\n"
        "assert config.max_orders_per_minute == 1\n"
    )
    checker = REPOSITORY_ROOT / ".venv" / ("Scripts/ty.exe" if os.name == "nt" else "bin/ty")
    if not checker.is_file():
        raise QualificationError("the external ty executable is unavailable")
    _run((str(checker), "check", "--python", str(python), str(consumer)), cwd=root)

    _run(("uv", "pip", "uninstall", "--python", str(python), "ml4t-live"), cwd=root)
    _run(
        (
            str(python),
            "-I",
            "-c",
            (
                "import importlib.util as i; "
                "assert i.find_spec('ml4t') is None or i.find_spec('ml4t.live') is None"
            ),
        ),
        cwd=root,
    )


def qualify_install_profiles(
    artifacts: Sequence[Path], expected_version: str, profiles_root: Path
) -> list[ProfileResult]:
    results: list[ProfileResult] = []
    for artifact in artifacts:
        artifact_type = "wheel" if artifact.suffix == ".whl" else "sdist"
        for python_version in SUPPORTED_PYTHONS:
            profile_root = profiles_root / f"{artifact_type}-py{python_version.replace('.', '')}"
            profile_root.mkdir(parents=True)
            print(f"installed profile: {artifact_type} on Python {python_version}", flush=True)
            try:
                install_profile(artifact, python_version, expected_version, profile_root)
            except Exception as error:
                print(f"installed profile failed: {artifact_type} Python {python_version}: {error}")
                results.append(ProfileResult(artifact_type, python_version, False, str(error)))
            else:
                results.append(ProfileResult(artifact_type, python_version, True))
    return results


def assert_rejected_python(artifacts: Sequence[Path], root: Path) -> None:
    rejection_markers = ("requires-python", "requires python", ">=3.12,<3.15", "3.15")
    for artifact in artifacts:
        artifact_type = "wheel" if artifact.suffix == ".whl" else "sdist"
        profile_root = root / f"reject-{artifact_type}-py315"
        profile_root.mkdir(parents=True)
        venv = profile_root / "venv"
        _run(
            ("uv", "venv", "--seed", "--python", REJECTED_PYTHON, str(venv)),
            cwd=profile_root,
        )
        python = _profile_python(venv)
        result = subprocess.run(
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--build-constraint",
                str(BUILD_CONSTRAINTS),
                str(artifact),
            ),
            cwd=profile_root,
            capture_output=True,
            text=True,
            check=False,
        )
        diagnostic = (result.stdout + result.stderr).lower()
        if result.returncode == 0 or not any(marker in diagnostic for marker in rejection_markers):
            raise QualificationError(f"{artifact_type} did not reject Python {REJECTED_PYTHON}")
        if "building pydantic-core" in diagnostic or "building orjson" in diagnostic:
            raise QualificationError(
                f"{artifact_type} resolved runtime dependencies before rejection"
            )
        _run(
            (
                str(python),
                "-I",
                "-c",
                (
                    "import importlib.util as i; "
                    "assert i.find_spec('ml4t') is None or i.find_spec('ml4t.live') is None"
                ),
            ),
            cwd=profile_root,
        )


def _default_evidence_root() -> Path | None:
    candidate = (
        REPOSITORY_ROOT.parent
        / "ml4t-live-dev"
        / ".workspace"
        / "work"
        / ("ml4t-live-release-readiness")
    )
    return candidate if candidate.exists() else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--artifacts-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args()

    status = _repository_status()
    if status and not args.allow_dirty:
        raise QualificationError("artifact qualification requires a clean fixed revision")

    commit = _git_output("rev-parse", "HEAD")
    epoch = _source_date_epoch()
    with tempfile.TemporaryDirectory(prefix="ml4t-live-artifact-qualification-") as temporary:
        root = Path(temporary)
        if args.artifacts_dir:
            first = distribution_pair(args.artifacts_dir.resolve())
            second = build_distributions(root / "reproduction", epoch)
        else:
            first = build_distributions(root / "build-one", epoch)
            second = build_distributions(root / "build-two", epoch)
        assert_reproducible(first, second)
        wheel, sdist = first
        validate_manifests(wheel, sdist)
        version = validate_artifact_metadata(wheel, sdist)

        profiles = qualify_install_profiles((wheel, sdist), version, root / "profiles")
        assert_rejected_python((wheel, sdist), root / "profiles")
        evidence_root = args.evidence_root or _default_evidence_root()
        secret_result = scan_release(REPOSITORY_ROOT, (wheel, sdist), evidence_root)

        failures = [profile for profile in profiles if not profile.passed]
        if secret_result.findings:
            for finding in secret_result.findings:
                print(
                    f"secret finding: pattern={finding.pattern} "
                    f"location_digest={finding.location_digest} "
                    f"occurrences={finding.occurrence_count}"
                )

        report = {
            "schema_version": 1,
            "commit": commit,
            "dirty_candidate": bool(status),
            "version": version,
            "reproducible": True,
            "artifacts": {
                "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
                "sdist": {"filename": sdist.name, "sha256": _sha256(sdist)},
            },
            "manifests_exact": True,
            "supported_python": list(SUPPORTED_PYTHONS),
            "rejected_python": REJECTED_PYTHON,
            "profiles": [asdict(profile) for profile in profiles],
            "installed_wheel_examples": sorted(DETERMINISTIC_EXAMPLES),
            "secret_scan": {
                "sources": secret_result.sources,
                "bytes_scanned": secret_result.bytes_scanned,
                "redacted_findings": len(secret_result.findings),
                "passed": not secret_result.findings,
                "evidence_included": evidence_root is not None,
            },
            "passed": not failures and not secret_result.findings,
        }
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(
            f"artifact qualification: {'PASS' if report['passed'] else 'FAIL'} "
            f"({len(profiles)} installed profiles, Python 3.15 rejected, "
            f"{secret_result.sources} secret-scan sources)"
        )
        return int(not report["passed"])


if __name__ == "__main__":
    raise SystemExit(main())
