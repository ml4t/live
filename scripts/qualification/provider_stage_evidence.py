"""Validate reviewed historical duration evidence separately from current compatibility."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from scripts.qualification.provider_contract import (
    ProviderContractError,
    provider_contract,
    provider_stage_inputs,
)
from scripts.qualification.qualify_paper import PaperQualificationError, _verify_installed_candidate

COMPATIBILITY_PACKAGES = {
    "ib": ("ib-async", "aeventkit", "numpy", "tzdata"),
    "alpaca": (
        "alpaca-py",
        "websockets",
        "pydantic",
        "pydantic-core",
        "requests",
        "urllib3",
        "certifi",
        "charset-normalizer",
        "idna",
        "python-dateutil",
        "pytz",
        "annotated-types",
        "typing-extensions",
        "typing-inspection",
        "numpy",
        "pandas",
    ),
    "okx": ("httpx", "httpcore", "anyio", "idna", "certifi", "h11", "typing-extensions"),
}


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def input_digest(inputs: dict[str, Any]) -> str:
    return digest(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode())


def installer_runtime(payload: bytes) -> dict[str, Any]:
    """Read actual native installer observations, without substituting a source lock."""
    text = payload.decode("utf-8-sig")
    pythons = set(re.findall(r"Using Python (3\.12\.\d+) environment", text))
    packages = {}
    for name, version in re.findall(r" \+ ([A-Za-z0-9_.-]+)==([^\s]+)", text):
        canonical = name.lower().replace("_", "-")
        if canonical in packages and packages[canonical] != version:
            raise PaperQualificationError("runtime log contains conflicting installed versions")
        packages[canonical] = version
    if (
        len(pythons) != 1
        or not {
            "ml4t-live",
            "ml4t-backtest",
            "ml4t-specs",
            "psutil",
            "ib-async",
            "aeventkit",
            "numpy",
            "pandas",
            "alpaca-py",
            "websockets",
            "pydantic",
            "httpx",
            "httpcore",
            "anyio",
        }
        <= packages.keys()
    ):
        raise PaperQualificationError("missing actual provider runtime observations")
    return {"python": pythons.pop(), "packages": packages}


def current_candidate_runtime(
    candidate: dict[str, Any], artifacts_directory: Path, checkout_root: Path
) -> dict[str, Any]:
    """Bind the installed runtime to the current wheel before selecting retained stages."""
    _verify_installed_candidate(candidate, checkout_root)
    import ml4t.live

    if ml4t.live.__version__ != candidate["version"]:
        raise PaperQualificationError("installed public version differs from candidate metadata")
    package_root = Path(ml4t.live.__file__).resolve().parent
    distribution_root = Path(
        importlib.metadata.distribution("ml4t-live").locate_file("ml4t/live")
    ).resolve()
    if package_root != distribution_root:
        raise PaperQualificationError(
            "current candidate was not imported from its installed distribution"
        )
    wheel = artifacts_directory / candidate["wheel"]["filename"]
    if digest(wheel.read_bytes()) != candidate["wheel"]["sha256"]:
        raise PaperQualificationError("current candidate wheel digest changed")
    sdist = artifacts_directory / candidate["sdist"]["filename"]
    if digest(sdist.read_bytes()) != candidate["sdist"]["sha256"]:
        raise PaperQualificationError("current candidate source distribution digest changed")
    tracked = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", candidate["commit"], "--", "src/ml4t/live"],
        cwd=checkout_root,
        text=True,
    ).splitlines()
    with zipfile.ZipFile(wheel) as archive:
        payload = {
            name
            for name in archive.namelist()
            if name.startswith("ml4t/live/") and not name.endswith("/")
        }
        if payload - {"ml4t/live/_version.py"} != {name.removeprefix("src/") for name in tracked}:
            raise PaperQualificationError("current candidate wheel source manifest differs")
        for name in archive.namelist():
            if name.startswith("ml4t/live/") and not name.endswith("/"):
                installed = package_root / name.removeprefix("ml4t/live/")
                if not installed.is_file() or installed.read_bytes() != archive.read(name):
                    raise PaperQualificationError("installed candidate payload differs from wheel")
                if name != "ml4t/live/_version.py":
                    original = subprocess.check_output(
                        ["git", "show", f"{candidate['commit']}:src/{name}"],
                        cwd=checkout_root,
                    )
                    if original != archive.read(name):
                        raise PaperQualificationError(
                            "candidate wheel differs from its recorded source"
                        )
    return {
        "python": ".".join(str(value) for value in sys.version_info[:3]),
        "packages": {
            item.metadata["Name"].lower().replace("_", "-"): item.version
            for item in importlib.metadata.distributions()
            if item.metadata["Name"]
        },
    }


def validate_stage_review(
    review: dict[str, Any],
    *,
    provider: str,
    candidate_commit: str,
    checkout_root: Path,
    baseline_report: dict[str, Any],
    baseline_report_bytes: bytes,
    baseline_log: bytes,
    compatibility_commit: str,
    compatibility_log: bytes,
    current_runtime: dict[str, Any],
) -> dict[str, Any]:
    """Fail closed unless a reviewed baseline and compatible current operations still match."""
    baseline = review["baseline"]
    if (
        review.get("stage") != "connection-duration"
        or review.get("provider") != provider
        or not review.get("rationale")
        or not review.get("review_evidence")
        or digest(baseline_report_bytes) != baseline.get("report_sha256")
        or baseline_report["candidate"]["commit"] != baseline.get("commit")
        or digest(baseline_log) != baseline.get("runtime_log_sha256")
    ):
        raise PaperQualificationError(
            "duration review does not bind its original report and runtime"
        )
    original_contract = baseline_report.get("provider_contract", {})
    if original_contract != provider_contract(
        provider,
        checkout_root=checkout_root,
        ref=baseline["commit"],
        schema_version=original_contract.get("schema_version", 0),
    ):
        raise PaperQualificationError("duration report contract differs from its original source")
    original_runtime = installer_runtime(baseline_log)
    if original_runtime["packages"].get("ml4t-live") != baseline_report["candidate"]["version"]:
        raise PaperQualificationError("duration artifact and installed version differ")
    dependency = original_contract["dependency"]
    if original_runtime["packages"].get(dependency["name"]) != dependency["version"]:
        raise PaperQualificationError("original installed SDK differs from embedded contract")
    compatible_runtime = installer_runtime(compatibility_log)
    if original_runtime != review.get("original_runtime"):
        raise PaperQualificationError("review is missing or misstates actual original runtime")
    if compatible_runtime != review.get("compatible_runtime"):
        raise PaperQualificationError("compatibility runtime has an unreviewed change")
    expected_packages = compatible_runtime["packages"]
    required = (*COMPATIBILITY_PACKAGES[provider], "ml4t-backtest", "ml4t-specs", "psutil")
    if not str(current_runtime.get("python", "")).startswith("3.12.") or any(
        name not in expected_packages
        or current_runtime.get("packages", {}).get(name) != expected_packages[name]
        for name in required
    ):
        raise PaperQualificationError(
            "current installed runtime differs from reviewed compatibility"
        )
    changes = {
        name: {
            "original": original_runtime["packages"].get(name),
            "current": expected_packages.get(name),
        }
        for name in original_runtime["packages"].keys() | expected_packages.keys()
        if original_runtime["packages"].get(name) != expected_packages.get(name)
    }
    decisions = review.get("dependency_changes", {})
    if set(decisions) != set(changes) or any(
        decisions[name].get("original") != values["original"]
        or decisions[name].get("current") != values["current"]
        or not decisions[name].get("rationale")
        or not decisions[name].get("evidence")
        for name, values in changes.items()
    ):
        raise PaperQualificationError("runtime changes lack a complete impact review")
    try:
        for ref, expected in (
            (baseline["commit"], review.get("baseline_duration_inputs_sha256")),
            (candidate_commit, review.get("candidate_duration_inputs_sha256")),
        ):
            if (
                input_digest(
                    provider_stage_inputs(
                        provider, "connection-duration", checkout_root=checkout_root, ref=ref
                    )
                )
                != expected
            ):
                raise PaperQualificationError("reached connection-duration operations changed")
        if review.get("baseline_duration_inputs_sha256") != review.get(
            "candidate_duration_inputs_sha256"
        ) and not review.get("source_impact_rationale"):
            raise PaperQualificationError("changed duration inputs lack an explicit impact review")
        if compatibility_commit != review["compatibility"]["commit"]:
            raise PaperQualificationError("compatibility source differs from its review binding")
        compatible_inputs = provider_stage_inputs(
            provider, "compatibility", checkout_root=checkout_root, ref=compatibility_commit
        )
        current_inputs = provider_stage_inputs(
            provider, "compatibility", checkout_root=checkout_root, ref=candidate_commit
        )
        if compatible_inputs != current_inputs or input_digest(compatible_inputs) != review.get(
            "compatibility_inputs_sha256"
        ):
            raise PaperQualificationError("reached compatibility operations changed")
    except ProviderContractError as error:
        raise PaperQualificationError(str(error)) from error
    return {
        "stage": "connection-duration",
        "original_candidate": baseline_report["candidate"],
        "report_sha256": digest(baseline_report_bytes),
        "original_runtime": original_runtime,
        "compatibility_runtime": compatible_runtime,
        "current_runtime": current_runtime,
        "review_rationale": review["rationale"],
        "review_sha256": digest(json.dumps(review, sort_keys=True).encode()),
    }
