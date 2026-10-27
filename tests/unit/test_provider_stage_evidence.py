from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.qualification.check_paper_evidence import find_reviewed_stage_evidence
from scripts.qualification.provider_contract import provider_stage_inputs
from scripts.qualification.provider_stage_evidence import digest, input_digest, installer_runtime

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/provider-evidence"
ORIGINAL = json.loads((FIXTURES / "provenance.json").read_text())["original"]
CURRENT = json.loads((FIXTURES / "provenance.json").read_text())["current"]


def services():
    def fetch(url, token):
        if "/actions/runs/" in url:
            run_id = int(url.rsplit("/", 1)[-1])
            item = ORIGINAL if run_id == ORIGINAL["run_id"] else CURRENT
            assert run_id == item["run_id"]
            return {
                "id": run_id,
                "head_sha": item["source"],
                "status": "completed",
                "conclusion": "success" if item is ORIGINAL else "failure",
                "artifacts_url": f"fixture://artifacts/{run_id}",
            }
        if url.startswith("fixture://artifacts/"):
            run_id = int(url.rsplit("/", 1)[-1])
            item = ORIGINAL if run_id == ORIGINAL["run_id"] else CURRENT
            return {
                "artifacts": [
                    {
                        "name": f"paper-{item['source']}-{run_id}",
                        "expired": False,
                        "archive_download_url": f"fixture://archive/{run_id}",
                    }
                ]
            }
        if "/actions/jobs/" in url:
            job_id = int(url.rsplit("/", 1)[-1])
            item = ORIGINAL if job_id == ORIGINAL["job_id"] else CURRENT
            assert item["job_id"] == job_id
            return {"run_id": item["run_id"], "head_sha": item["source"]}
        raise AssertionError(url)

    def download(url, token):
        if url.startswith("fixture://archive/"):
            name = (
                "original-duration.zip"
                if url.endswith(str(ORIGINAL["run_id"]))
                else "current-compatibility.zip"
            )
        else:
            name = (
                "original-installer.txt"
                if f"/{ORIGINAL['job_id']}/" in url
                else "current-installer.txt"
            )
        return (
            (FIXTURES / name).read_bytes()
            if name.endswith(".zip")
            else (FIXTURES / name).read_text().encode()
        )

    return fetch, download


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(root)], check=True
    )
    subprocess.run(["git", "checkout", "--quiet", "HEAD"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Evidence regression"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "evidence@example.test"], cwd=root, check=True)
    return root


def commit(root, path, before, after):
    target = root / path
    original = target.read_text()
    changed = original.replace(before, after)
    assert changed != original
    target.write_text(changed)
    subprocess.run(["git", "add", path], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture: change producing code"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def current_review():
    review = json.loads((ROOT / "provider-evidence-reviews.json").read_text())
    review["common"]["baseline"]["runtime_log_sha256"] = digest(
        (FIXTURES / "original-installer.txt").read_text().encode()
    )
    review["common"]["compatibility"]["runtime_log_sha256"] = digest(
        (FIXTURES / "current-installer.txt").read_text().encode()
    )
    return review


def select(root, review, *, runtime=None, downloader=None):
    fetch, download = services()
    return find_reviewed_stage_evidence(
        repository="ml4t/live",
        commit="HEAD",
        token="fixture",
        checkout_root=root,
        candidate_artifacts={"wheel": {"sha256": "d" * 64}, "sdist": {"sha256": "e" * 64}},
        reviews=review,
        current_runtime=runtime or review["common"]["compatible_runtime"],
        fetcher=fetch,
        downloader=downloader or download,
    )


def test_native_current_short_results_select_only_valid_independent_provider_stages(repository):
    result = select(repository, current_review())
    assert not result["passed"]
    assert set(result["providers"]) == {"alpaca", "okx"}
    assert result["failures"] == {"ib": "fresh current compatibility evidence is missing"}
    for item in result["providers"].values():
        assert item["connection_duration"]["original_candidate"]["commit"] == ORIGINAL["source"]
        assert item["connection_duration"]["snapshot_count"] == 73
        assert item["compatibility"]["run_id"] == CURRENT["run_id"]


def test_unrelated_calendar_behavior_preserves_native_broker_duration_and_compatibility(repository):
    commit(
        repository,
        "src/ml4t/live/temporal.py",
        "instant < session.closing",
        "instant <= session.closing",
    )
    result = select(repository, current_review())
    assert set(result["providers"]) == {"alpaca", "okx"}
    assert result["providers"]["alpaca"]["connection_duration"]["duration_seconds"] > 21600
    assert set(result["failures"]) == {"ib"}


def test_qualification_formatting_does_not_invalidate_native_duration(repository):
    commit(
        repository,
        "scripts/qualification/qualify_paper.py",
        "await asyncio.sleep(remaining)",
        "await asyncio.sleep(\n            remaining,\n        )",
    )
    result = select(repository, current_review())
    assert set(result["providers"]) == {"alpaca", "okx"}
    assert set(result["failures"]) == {"ib"}


def test_changed_qualification_sleep_behavior_rejects_native_broker_duration(repository):
    commit(
        repository,
        "scripts/qualification/qualify_paper.py",
        "await asyncio.sleep(remaining)",
        "await asyncio.sleep(0)",
    )
    result = select(repository, current_review())
    assert set(result["providers"]) == {"okx"}
    assert "operations changed" in result["failures"]["alpaca"]


def test_original_native_stages_are_selected_after_only_calendar_clock_changes(repository):
    subprocess.run(["git", "checkout", "--quiet", ORIGINAL["source"]], cwd=repository, check=True)
    commit(
        repository,
        "src/ml4t/live/engine.py",
        "US_EQUITY_CLOSE = dt_time(16, 0)",
        "US_EQUITY_CLOSE = dt_time(15, 0)",
    )
    review = json.loads((FIXTURES / "original-operations-review.json").read_text())
    result = select(repository, review)
    assert result["passed"]
    assert set(result["providers"]) == {"alpaca", "ib", "okx"}
    assert result["wheel_sha256"] == "d" * 64
    for item in result["providers"].values():
        assert item["connection_duration"]["original_candidate"]["commit"] == ORIGINAL["source"]
        assert item["connection_duration"]["original_runtime"]["packages"]["numpy"] == "2.5.4"


def test_changed_broker_connection_behavior_rejects_only_reached_provider_stages(repository):
    commit(
        repository,
        "src/ml4t/live/brokers/alpaca.py",
        "self._connected = True",
        "self._connected = False",
    )
    result = select(repository, current_review())
    assert set(result["providers"]) == {"okx"}
    assert "operations changed" in result["failures"]["alpaca"]


def test_refreshed_current_hash_cannot_reuse_stale_short_after_cash_producer_breaks(repository):
    revision = commit(
        repository,
        "src/ml4t/live/brokers/alpaca.py",
        'return self._validate_account_metric(account, "cash", allow_negative=True)',
        'return float("nan")',
    )
    review = current_review()
    alpaca = review["providers"]["alpaca"]
    alpaca["candidate_duration_inputs_sha256"] = input_digest(
        provider_stage_inputs(
            "alpaca", "connection-duration", checkout_root=repository, ref=revision
        )
    )
    alpaca["compatibility_inputs_sha256"] = input_digest(
        provider_stage_inputs("alpaca", "compatibility", checkout_root=repository, ref=revision)
    )
    alpaca["source_impact_rationale"] = (
        "Attempted duration review cannot waive current compatibility"
    )
    result = select(repository, review)
    assert set(result["providers"]) == {"okx"}
    assert "compatibility operations changed" in result["failures"]["alpaca"]


@pytest.mark.parametrize(
    "mutation", ["archive", "report", "source", "runtime", "dependency_review"]
)
def test_altered_duration_or_missing_provenance_never_selects_historical_stages(
    repository, mutation
):
    review = current_review()
    if mutation == "archive":
        review["common"]["baseline"]["archive_sha256"] = "0" * 64
    elif mutation == "report":
        review["providers"]["alpaca"]["baseline"]["report_sha256"] = "0" * 64
    elif mutation == "source":
        review["common"]["baseline"]["commit"] = CURRENT["source"]
    elif mutation == "runtime":
        del review["common"]["original_runtime"]["packages"]["numpy"]
    else:
        del review["common"]["dependency_changes"]["websockets"]
    result = select(repository, review)
    assert "alpaca" not in result["providers"]
    assert not result["passed"]


def test_actual_unreviewed_prior_transport_stack_does_not_satisfy_current_compatibility(repository):
    review = current_review()
    previous_runtime = installer_runtime((FIXTURES / "original-installer.txt").read_text().encode())
    result = select(repository, review, runtime=previous_runtime)
    assert not result["providers"]
    assert "current installed runtime differs" in result["failures"]["alpaca"]


def test_missing_native_runtime_log_cannot_be_replaced_with_source_lock(repository):
    review = current_review()
    _, download = services()
    result = select(
        repository,
        review,
        downloader=lambda url, token: b"" if url.endswith("/logs") else download(url, token),
    )
    assert not result["providers"]
    assert "runtime proof digest" in result["failures"]["okx"]


def test_actual_partial_native_outcomes_do_not_pass_missing_ib_or_duration():
    from scripts.qualification.check_paper_outcomes import outcome_failures

    outcomes = json.loads((FIXTURES / "provenance.json").read_text())["current"]["actual_outcomes"]
    assert outcome_failures(outcomes, "alpaca", stage="compatibility") == []
    assert outcome_failures(outcomes, "okx", stage="compatibility") == []
    assert outcome_failures(outcomes, "all", stage="compatibility") == ["ib-exercise", "ib-restart"]
    assert outcome_failures(outcomes, "alpaca", stage="complete") == ["provider-soaks"]


def test_changed_reached_okx_transport_timeout_rejects_retained_duration(repository):
    commit(
        repository,
        "src/ml4t/live/feeds/okx_feed.py",
        "httpx.AsyncClient(timeout=30.0)",
        "httpx.AsyncClient(timeout=None)",
    )
    result = select(repository, current_review())
    assert set(result["providers"]) == {"alpaca"}
    assert "operations changed" in result["failures"]["okx"]


def test_repacked_archive_cannot_relabel_a_valid_original_measurement(repository):
    import io
    import zipfile

    review = current_review()
    buffer = io.BytesIO()
    with zipfile.ZipFile(FIXTURES / "original-duration.zip") as original:
        with zipfile.ZipFile(buffer, "w") as altered:
            for name in original.namelist():
                raw = original.read(name)
                if name.endswith("alpaca-soak.json"):
                    report = json.loads(raw)
                    report["duration_seconds"] += 1
                    raw = json.dumps(report).encode()
                altered.writestr(name, raw)
    payload = buffer.getvalue()
    review["common"]["baseline"]["archive_sha256"] = digest(payload)
    _, download = services()
    result = select(
        repository,
        review,
        downloader=lambda url, token: (
            payload if url == f"fixture://archive/{ORIGINAL['run_id']}" else download(url, token)
        ),
    )
    assert "alpaca" not in result["providers"]
    assert "original report and runtime" in result["failures"]["alpaca"]


def test_unreached_ccxt_signing_dependencies_do_not_invalidate_httpx_or_broker_stages(repository):
    from copy import deepcopy

    review = current_review()
    observed = deepcopy(review["common"]["compatible_runtime"])
    original = review["common"]["original_runtime"]["packages"]
    for name in ("cryptography", "orjson"):
        observed["packages"][name] = original[name]
    result = select(repository, review, runtime=observed)
    assert set(result["providers"]) == {"alpaca", "okx"}
    assert set(result["failures"]) == {"ib"}
