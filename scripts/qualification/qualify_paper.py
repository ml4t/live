"""Qualify IB and Alpaca paper accounts against one installed candidate wheel."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil

GITHUB_API = "https://api.github.com"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROVIDERS = ("alpaca", "ib")
PHASES = ("exercise", "restart")
ASSET = "AAPL"
QUANTITY = 1.0
INITIAL_LIMIT = 1.00
REPLACEMENT_LIMIT = 1.01
SOAK_DURATION_SECONDS = 6 * 60 * 60
SOAK_SNAPSHOT_INTERVAL_SECONDS = 5 * 60
SOAK_RSS_GROWTH_LIMIT_BYTES = 25 * 1024 * 1024
SOAK_SHUTDOWN_LIMIT_SECONDS = 5.0
EXERCISE_STEP_SEQUENCE = (
    "installed_candidate",
    "connect",
    "paper_identity",
    "positions",
    "filtered_pending_orders",
    "cash",
    "account_value",
    "capability_rejection",
    "policy_rejection",
    "submit",
    "working_acknowledgement",
    "reconnect",
    "reconnect_reconciliation",
    "replace",
    "cancel",
    "cleanup",
)
RESTART_STEP_SEQUENCE = (
    "installed_candidate",
    "connect",
    "paper_identity",
    "positions",
    "filtered_pending_orders",
    "cash",
    "account_value",
    "restart_reconciliation",
    "cleanup",
)


class PaperQualificationError(RuntimeError):
    """The candidate or paper-account qualification contract failed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text())
    if not isinstance(loaded, dict):
        raise PaperQualificationError(f"{path.name} must contain a JSON object")
    return loaded


def _fetch_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        loaded = json.load(response)
    if not isinstance(loaded, dict):
        raise PaperQualificationError("GitHub returned a non-object response")
    return loaded


def _wheel_version(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise PaperQualificationError("candidate wheel has no unique METADATA file")
        for line in archive.read(metadata_files[0]).decode().splitlines():
            if line.startswith("Version: "):
                return line.removeprefix("Version: ").strip()
    raise PaperQualificationError("candidate wheel metadata has no version")


def build_candidate_manifest(
    *,
    artifacts_directory: Path,
    candidate_sha: str,
    qualification_run_id: int,
    repository: str,
    run_record: dict[str, Any],
) -> dict[str, Any]:
    """Bind one successful qualification run and one artifact pair to an exact commit."""
    if not COMMIT_PATTERN.fullmatch(candidate_sha):
        raise PaperQualificationError("candidate SHA must be a full lowercase commit hash")
    if run_record.get("head_sha") != candidate_sha:
        raise PaperQualificationError("qualification run does not target the candidate commit")
    if run_record.get("conclusion") != "success" or run_record.get("status") != "completed":
        raise PaperQualificationError("qualification run is not complete and successful")
    if run_record.get("name") not in {"CI", "Reusable stable qualification"}:
        raise PaperQualificationError(
            "artifact did not come from an authoritative qualification workflow"
        )
    if int(run_record.get("id", 0)) != qualification_run_id:
        raise PaperQualificationError("qualification run identity changed")
    wheels = sorted(artifacts_directory.glob("*.whl"))
    sdists = sorted(artifacts_directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise PaperQualificationError(
            "candidate input must contain exactly one wheel and one sdist"
        )
    wheel, sdist = wheels[0], sdists[0]
    return {
        "schema_version": 1,
        "repository": repository,
        "commit": candidate_sha,
        "qualification_run_id": qualification_run_id,
        "version": _wheel_version(wheel),
        "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
        "sdist": {"filename": sdist.name, "sha256": _sha256(sdist)},
        "passed": True,
    }


def verify_candidate_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "repository",
        "commit",
        "qualification_run_id",
        "version",
        "wheel",
        "sdist",
        "passed",
    }
    if set(manifest) != required or manifest.get("schema_version") != 1:
        raise PaperQualificationError("candidate manifest schema is invalid")
    if manifest.get("passed") is not True or not COMMIT_PATTERN.fullmatch(
        str(manifest.get("commit", ""))
    ):
        raise PaperQualificationError("candidate manifest does not identify a passing commit")
    if not isinstance(manifest.get("qualification_run_id"), int):
        raise PaperQualificationError("candidate qualification run ID is invalid")
    for artifact_name in ("wheel", "sdist"):
        artifact = manifest.get(artifact_name)
        if not isinstance(artifact, dict) or set(artifact) != {"filename", "sha256"}:
            raise PaperQualificationError(f"candidate {artifact_name} record is invalid")
        if not HASH_PATTERN.fullmatch(str(artifact.get("sha256", ""))):
            raise PaperQualificationError(f"candidate {artifact_name} hash is invalid")


def _candidate_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    verify_candidate_manifest(manifest)
    return {
        "commit": manifest["commit"],
        "qualification_run_id": manifest["qualification_run_id"],
        "version": manifest["version"],
        "wheel_sha256": manifest["wheel"]["sha256"],
        "sdist_sha256": manifest["sdist"]["sha256"],
    }


def _evidence_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PaperQualificationError("paper evidence timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PaperQualificationError("paper evidence timestamp is invalid") from error
    if parsed.utcoffset() is None:
        raise PaperQualificationError("paper evidence timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


def _verify_installed_candidate(candidate: dict[str, Any], checkout_root: Path) -> None:
    import ml4t.live

    installed_version = importlib.metadata.version("ml4t-live")
    if installed_version != candidate["version"]:
        raise PaperQualificationError(
            "installed ml4t-live version differs from the candidate wheel"
        )
    package_path = Path(ml4t.live.__file__).resolve()
    checkout = checkout_root.resolve()
    if package_path == checkout or checkout in package_path.parents:
        raise PaperQualificationError("paper qualification imported ml4t-live from the checkout")


def _require_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PaperQualificationError(f"required protected environment value is missing: {name}")
    return value


def _build_broker(provider: str) -> Any:
    if provider == "alpaca":
        from ml4t.live import AlpacaBroker

        return AlpacaBroker(
            api_key=_require_environment("ALPACA_API_KEY"),
            secret_key=_require_environment("ALPACA_SECRET_KEY"),
            paper=True,
        )
    from ml4t.live import IBBroker

    host = _require_environment("IB_HOST")
    try:
        port = int(_require_environment("IB_PORT"))
        client_id = int(_require_environment("IB_CLIENT_ID"))
    except ValueError as error:
        raise PaperQualificationError("IB_PORT and IB_CLIENT_ID must be integers") from error
    if port not in {4002, 7497}:
        raise PaperQualificationError("IB_PORT is not a standard paper-trading port")
    account = os.environ.get("IB_ACCOUNT", "").strip() or None
    return IBBroker(host=host, port=port, client_id=client_id, account=account)


def _number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise PaperQualificationError("provider snapshot contains a non-finite number")
    return number


def _pending_ib_statuses() -> frozenset[str]:
    from ml4t.live.brokers.ib import IB_PENDING_ORDER_STATUSES

    return IB_PENDING_ORDER_STATUSES


def _order_type(limit_price: Any, stop_price: Any, time_in_force: Any = None) -> str:
    if str(getattr(time_in_force, "value", time_in_force)).lower() == "cls":
        return "moc"
    if limit_price and stop_price:
        return "stop_limit"
    if limit_price:
        return "limit"
    if stop_price:
        return "stop"
    return "market"


def _adapter_snapshot(broker: Any) -> tuple[dict[str, float], Counter[tuple[Any, ...]]]:
    positions = {
        asset.upper(): _number(position.quantity) for asset, position in broker.positions.items()
    }
    orders: Counter[tuple[Any, ...]] = Counter()
    for order in broker.pending_orders:
        orders[
            (
                order.asset.upper(),
                _number(order.quantity),
                order.side.value,
                order.order_type.value,
                _number(order.limit_price) if order.limit_price is not None else None,
                _number(order.stop_price) if order.stop_price is not None else None,
            )
        ] += 1
    return positions, orders


def _vendor_snapshot(
    provider: str, broker: Any
) -> tuple[dict[str, float], Counter[tuple[Any, ...]]]:
    if provider == "alpaca":
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        client = broker._trading_client
        positions = {
            str(position.symbol).strip().upper(): _number(position.qty)
            for position in client.get_all_positions()
        }
        raw_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        orders: Counter[tuple[Any, ...]] = Counter()
        for order in raw_orders:
            orders[
                (
                    str(order.symbol).strip().upper(),
                    _number(order.qty),
                    str(getattr(order.side, "value", order.side)).lower(),
                    _order_type(order.limit_price, order.stop_price, order.time_in_force),
                    _number(order.limit_price) if order.limit_price else None,
                    _number(order.stop_price) if order.stop_price else None,
                )
            ] += 1
        return positions, orders

    raw_positions = broker.ib.positions()
    positions = {
        str(position.contract.symbol).strip().upper(): _number(position.position)
        for position in raw_positions
        if _number(position.position) != 0
    }
    orders = Counter()
    for trade in broker.ib.openTrades():
        if str(trade.orderStatus.status) not in _pending_ib_statuses():
            continue
        limit_price = getattr(trade.order, "lmtPrice", None)
        stop_price = getattr(trade.order, "auxPrice", None)
        if limit_price is not None and (
            not math.isfinite(float(limit_price)) or float(limit_price) >= 1e100
        ):
            limit_price = None
        if stop_price is not None and (
            not math.isfinite(float(stop_price)) or float(stop_price) >= 1e100
        ):
            stop_price = None
        ib_type = str(getattr(trade.order, "orderType", "")).upper()
        normalized_types = {
            "MKT": "market",
            "MOC": "moc",
            "LMT": "limit",
            "STP": "stop",
            "STP LMT": "stop_limit",
        }
        if ib_type not in normalized_types:
            raise PaperQualificationError("IB snapshot contains an unsupported working order type")
        normalized_type = normalized_types[ib_type]
        orders[
            (
                str(trade.contract.symbol).strip().upper(),
                _number(trade.order.totalQuantity),
                str(trade.order.action).lower(),
                normalized_type,
                _number(limit_price) if limit_price else None,
                _number(stop_price) if stop_price else None,
            )
        ] += 1
    return positions, orders


async def _snapshot(provider: str, broker: Any) -> dict[str, Any]:
    await broker._sync_positions()
    await broker._sync_orders()
    adapter_positions, adapter_orders = _adapter_snapshot(broker)
    vendor_positions, vendor_orders = _vendor_snapshot(provider, broker)
    filtered = await broker.get_pending_orders_async(ASSET)
    expected_filtered = sum(count for order, count in adapter_orders.items() if order[0] == ASSET)
    if adapter_positions != vendor_positions:
        raise PaperQualificationError("adapter positions differ from the provider snapshot")
    if adapter_orders != vendor_orders:
        raise PaperQualificationError("adapter pending orders differ from the provider snapshot")
    if len(filtered) != expected_filtered:
        raise PaperQualificationError("asset-filtered pending orders are incomplete")
    account_value = _number(await broker.get_account_value_async())
    _number(await broker.get_cash_async())
    if account_value <= 0:
        raise PaperQualificationError("paper account metrics are invalid")
    return {
        "positions_count": len(adapter_positions),
        "pending_orders_count": sum(adapter_orders.values()),
        "filtered_pending_orders_count": len(filtered),
        "position_snapshot_exact": True,
        "pending_order_snapshot_exact": True,
        "account_value_valid": True,
        "cash_valid": True,
    }


def _raw_tagged_orders(provider: str, broker: Any, tags: set[str]) -> list[Any]:
    if provider == "alpaca":
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = broker._trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return [order for order in orders if getattr(order, "client_order_id", None) in tags]
    return [
        trade
        for trade in broker.ib.openTrades()
        if str(getattr(trade.order, "orderRef", "")) in tags
        and str(trade.orderStatus.status) in _pending_ib_statuses()
    ]


def _adapter_id_for_tag(provider: str, broker: Any, tag: str) -> str:
    matches = _raw_tagged_orders(provider, broker, {tag})
    if len(matches) != 1:
        raise PaperQualificationError("qualification tag does not identify one working order")
    if provider == "alpaca":
        venue_id = str(matches[0].id)
        mapping = broker._alpaca_order_map.get(venue_id)
    else:
        venue_id = int(matches[0].order.orderId)
        mapping = broker._ib_order_map.get(venue_id)
    if mapping is None:
        raise PaperQualificationError("working qualification order is absent from adapter state")
    return str(mapping[0])


async def _wait_for_tag_count(
    provider: str, broker: Any, tags: set[str], expected: int, timeout: float = 30.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_raw_tagged_orders(provider, broker, tags)) == expected:
            return
        await asyncio.sleep(0.25)
    raise PaperQualificationError("provider did not reach the expected tagged-order state")


def _tag_keyword(provider: str, tag: str) -> dict[str, Any]:
    if provider == "alpaca":
        return {"client_order_id": tag}
    return {"order_ref": tag, "outsideRth": True}


async def _assert_atomic_rejections(provider: str, broker: Any, state_directory: Path) -> None:
    from ml4t.backtest.types import OrderSide, OrderType

    from ml4t.live import LiveRiskConfig, RiskLimitError, SafeBroker

    state_before = _vendor_snapshot(provider, broker)
    submission_target = broker._trading_client if provider == "alpaca" else broker.ib
    method_name = "submit_order" if provider == "alpaca" else "placeOrder"
    original = getattr(submission_target, method_name)
    venue_calls = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        nonlocal venue_calls
        venue_calls += 1
        return original(*args, **kwargs)

    setattr(submission_target, method_name, counted)
    try:
        try:
            await broker.submit_order_async(
                ASSET,
                QUANTITY,
                side=OrderSide.BUY,
                order_type=OrderType.TRAILING_STOP,
                stop_price=INITIAL_LIMIT,
            )
        except (NotImplementedError, ValueError):
            pass
        else:
            raise PaperQualificationError("unsupported capability was accepted")
        if venue_calls:
            raise PaperQualificationError("capability rejection reached the provider")

        state_directory.mkdir(parents=True, exist_ok=True)
        safe = SafeBroker(
            broker,
            LiveRiskConfig(
                execution_mode="paper",
                allowed_assets={"MSFT"},
                state_file=str(state_directory / f"{provider}-risk.json"),
                journal_file=str(state_directory / f"{provider}-journal.jsonl"),
            ),
        )
        try:
            try:
                await safe.submit_order_async(
                    ASSET,
                    QUANTITY,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    limit_price=INITIAL_LIMIT,
                )
            except RiskLimitError:
                pass
            else:
                raise PaperQualificationError("risk policy rejection was accepted")
        finally:
            safe.close_persistence()
        if venue_calls:
            raise PaperQualificationError("policy rejection reached the provider")
        if _vendor_snapshot(provider, broker) != state_before:
            raise PaperQualificationError("atomic rejection changed provider state")
    finally:
        setattr(submission_target, method_name, original)


async def _cleanup_tags(provider: str, broker: Any, tags: set[str]) -> bool:
    if not broker.is_connected:
        try:
            await broker.connect()
        except Exception:
            return False
    try:
        broker.assert_paper_trading()
    except Exception:
        return False
    for _ in range(3):
        matches = _raw_tagged_orders(provider, broker, tags)
        if not matches:
            return True
        for match in matches:
            if provider == "alpaca":
                mapping = broker._alpaca_order_map.get(str(match.id))
            else:
                mapping = broker._ib_order_map.get(int(match.order.orderId))
            if mapping is not None:
                await broker.cancel_order_async(str(mapping[0]))
        try:
            await _wait_for_tag_count(provider, broker, tags, 0, timeout=15.0)
            return True
        except PaperQualificationError:
            await broker._sync_orders()
    return not _raw_tagged_orders(provider, broker, tags)


async def _exercise_provider(
    provider: str,
    broker: Any,
    tags: tuple[str, str],
    state_directory: Path,
    steps: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from ml4t.backtest.types import OrderSide, OrderStatus, OrderType

    await broker.connect()
    steps.append("connect")
    broker.assert_paper_trading()
    steps.append("paper_identity")
    initial = await _snapshot(provider, broker)
    initial_position_state = _vendor_snapshot(provider, broker)[0]
    steps.extend(["positions", "filtered_pending_orders", "cash", "account_value"])
    await _assert_atomic_rejections(provider, broker, state_directory)
    steps.extend(["capability_rejection", "policy_rejection"])

    order = await broker.submit_order_async(
        ASSET,
        QUANTITY,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=INITIAL_LIMIT,
        **_tag_keyword(provider, tags[0]),
    )
    if order.status is not OrderStatus.PENDING:
        raise PaperQualificationError("minimal qualification order was not acknowledged as working")
    steps.append("submit")
    await _wait_for_tag_count(provider, broker, {tags[0]}, 1)
    steps.append("working_acknowledgement")

    await broker.disconnect()
    await broker.connect()
    broker.assert_paper_trading()
    reconnect = await _snapshot(provider, broker)
    await _wait_for_tag_count(provider, broker, {tags[0]}, 1)
    steps.extend(["reconnect", "reconnect_reconciliation"])

    original_id = _adapter_id_for_tag(provider, broker, tags[0])
    replacement = await broker.replace_order_async(
        original_id,
        limit_price=REPLACEMENT_LIMIT,
        **_tag_keyword(provider, tags[1]),
    )
    if replacement.status is not OrderStatus.PENDING:
        raise PaperQualificationError("replacement order was not acknowledged as working")
    await _wait_for_tag_count(provider, broker, {tags[0]}, 0)
    await _wait_for_tag_count(provider, broker, {tags[1]}, 1)
    steps.append("replace")

    replacement_id = _adapter_id_for_tag(provider, broker, tags[1])
    if not await broker.cancel_order_async(replacement_id):
        raise PaperQualificationError("replacement cancellation request was rejected")
    await _wait_for_tag_count(provider, broker, set(tags), 0)
    steps.append("cancel")
    final = await _snapshot(provider, broker)
    if _vendor_snapshot(provider, broker)[0] != initial_position_state:
        raise PaperQualificationError("qualification changed the paper position state")
    steps.append("cleanup")
    return initial, reconnect, final


async def _restart_provider(
    provider: str, broker: Any, tags: tuple[str, str], steps: list[str]
) -> dict[str, Any]:
    await broker.connect()
    steps.append("connect")
    broker.assert_paper_trading()
    steps.append("paper_identity")
    snapshot = await _snapshot(provider, broker)
    steps.extend(
        [
            "positions",
            "filtered_pending_orders",
            "cash",
            "account_value",
            "restart_reconciliation",
        ]
    )
    if _raw_tagged_orders(provider, broker, set(tags)):
        raise PaperQualificationError("fresh process found an uncleaned qualification order")
    steps.append("cleanup")
    return snapshot


async def run_provider_phase(
    *,
    provider: str,
    phase: str,
    candidate: dict[str, Any],
    checkout_root: Path,
    state_directory: Path,
    tag_seed: str,
) -> dict[str, Any]:
    """Run one provider phase while retaining only identifier-free evidence."""
    if provider not in PROVIDERS or phase not in PHASES:
        raise PaperQualificationError("unsupported provider phase")
    if not re.fullmatch(r"[a-z0-9]{8,20}", tag_seed):
        raise PaperQualificationError("qualification tag seed is invalid")
    identity = _candidate_identity(candidate)
    _verify_installed_candidate(identity, checkout_root)
    tags = (f"ml4tq-{tag_seed}-a", f"ml4tq-{tag_seed}-b")
    started_at = datetime.now(UTC).isoformat()
    steps: list[str] = ["installed_candidate"]
    snapshots: dict[str, Any] = {}
    passed = False
    failed_stage: str | None = None
    cleanup_passed = False
    broker: Any | None = None
    try:
        broker = _build_broker(provider)
        if phase == "exercise":
            initial, reconnect, final = await _exercise_provider(
                provider, broker, tags, state_directory, steps
            )
            snapshots = {"initial": initial, "reconnect": reconnect, "final": final}
        else:
            snapshots = {"restart": await _restart_provider(provider, broker, tags, steps)}
        passed = True
    except Exception as error:
        sequence = EXERCISE_STEP_SEQUENCE if phase == "exercise" else RESTART_STEP_SEQUENCE
        failed_stage = next((stage for stage in sequence if stage not in steps), "finalization")
        from ml4t.live.persistence import redact_sensitive

        detail = redact_sensitive(str(error))
        print(
            f"paper {provider} {phase} failed after {failed_stage}: "
            f"{type(error).__name__}: {detail}",
            file=sys.stderr,
        )
    finally:
        if broker is not None:
            try:
                cleanup_passed = await _cleanup_tags(provider, broker, set(tags))
            except Exception:
                cleanup_passed = False
            if not cleanup_passed and failed_stage is None:
                failed_stage = "cleanup"
            try:
                await broker.disconnect()
            except Exception:
                passed = False
                if failed_stage is None:
                    failed_stage = "disconnect"
        passed = passed and cleanup_passed
    return {
        "schema_version": 1,
        "provider": provider,
        "phase": phase,
        "candidate": identity,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "paper_identity_verified_before_submission": "paper_identity" in steps,
        "steps_passed": steps,
        "snapshots": snapshots,
        "cleanup_passed": cleanup_passed,
        "failed_stage": failed_stage,
        "passed": passed,
    }


def _provider_state_checksum(provider: str, broker: Any) -> str:
    positions, orders = _vendor_snapshot(provider, broker)
    state = {
        "positions": sorted(positions.items()),
        "orders": sorted([list(order) + [count] for order, count in orders.items()]),
    }
    encoded = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _rss_bytes() -> int:
    return psutil.Process().memory_info().rss


async def _soak_snapshot(provider: str, broker: Any, *, started_monotonic: float) -> dict[str, Any]:
    if not broker.is_connected:
        raise PaperQualificationError("paper provider disconnected during the soak")
    snapshot = await _snapshot(provider, broker)
    return {
        "elapsed_seconds": time.monotonic() - started_monotonic,
        "rss_bytes": _rss_bytes(),
        **snapshot,
        "connected": True,
    }


async def run_provider_soak(
    *, provider: str, candidate: dict[str, Any], checkout_root: Path
) -> dict[str, Any]:
    """Run a continuous paper session with one controlled reconnect."""
    if provider not in PROVIDERS:
        raise PaperQualificationError("unsupported paper soak provider")
    identity = _candidate_identity(candidate)
    _verify_installed_candidate(identity, checkout_root)
    started_at = datetime.now(UTC).isoformat()
    started_monotonic = time.monotonic()
    stage = "initialization"
    snapshots: list[dict[str, Any]] = []
    initial_checksum: str | None = None
    final_checksum: str | None = None
    paper_identity_verified = False
    reconnect_count = 0
    unexpected_disconnect_count = 0
    continuity_gap_count = 0
    maximum_shutdown_seconds = 0.0
    error_count = 0
    failure_type: str | None = None
    broker: Any | None = None
    passed = False
    try:
        broker = _build_broker(provider)
        stage = "connect"
        await broker.connect()
        broker.assert_paper_trading()
        paper_identity_verified = True
        stage = "initial_snapshot"
        snapshots.append(
            await _soak_snapshot(provider, broker, started_monotonic=started_monotonic)
        )
        initial_checksum = _provider_state_checksum(provider, broker)

        deadline = started_monotonic + SOAK_DURATION_SECONDS
        reconnect_at = started_monotonic + SOAK_DURATION_SECONDS / 2
        reconnected = False
        snapshot_total = math.ceil(SOAK_DURATION_SECONDS / SOAK_SNAPSHOT_INTERVAL_SECONDS - 1e-12)
        for snapshot_index in range(1, snapshot_total + 1):
            snapshot_at = min(
                started_monotonic + snapshot_index * SOAK_SNAPSHOT_INTERVAL_SECONDS,
                deadline,
            )
            if not reconnected and reconnect_at <= snapshot_at:
                await asyncio.sleep(max(0.0, reconnect_at - time.monotonic()))
                if not broker.is_connected:
                    unexpected_disconnect_count += 1
                    raise PaperQualificationError("paper provider disconnected during the soak")
                stage = "controlled_reconnect"
                shutdown_started = time.monotonic()
                await broker.disconnect()
                maximum_shutdown_seconds = max(
                    maximum_shutdown_seconds, time.monotonic() - shutdown_started
                )
                await broker.connect()
                broker.assert_paper_trading()
                reconnect_count += 1
                reconnected = True
            await asyncio.sleep(max(0.0, snapshot_at - time.monotonic()))
            if not broker.is_connected:
                unexpected_disconnect_count += 1
                raise PaperQualificationError("paper provider disconnected during the soak")
            stage = "scheduled_snapshot"
            snapshot = await _soak_snapshot(provider, broker, started_monotonic=started_monotonic)
            if snapshot["elapsed_seconds"] > (
                snapshot_at - started_monotonic + SOAK_SNAPSHOT_INTERVAL_SECONDS
            ):
                continuity_gap_count += 1
                raise PaperQualificationError("paper soak snapshot continuity was lost")
            snapshots.append(snapshot)

        stage = "final_reconciliation"
        final_checksum = _provider_state_checksum(provider, broker)
        passed = True
    except Exception as error:
        error_count += 1
        failure_type = type(error).__name__
        print(
            f"paper {provider} soak failed during {stage}: {failure_type}",
            file=sys.stderr,
        )
    finally:
        if broker is not None and broker.is_connected:
            try:
                stage = "disconnect"
                shutdown_started = time.monotonic()
                await broker.disconnect()
                maximum_shutdown_seconds = max(
                    maximum_shutdown_seconds, time.monotonic() - shutdown_started
                )
            except Exception as error:
                error_count += 1
                failure_type = type(error).__name__
                passed = False
        duration_seconds = time.monotonic() - started_monotonic

    state_unchanged = initial_checksum is not None and initial_checksum == final_checksum
    rss_values = [snapshot["rss_bytes"] for snapshot in snapshots]
    rss_growth_bytes = max(rss_values, default=0) - (rss_values[0] if rss_values else 0)
    passed = (
        passed
        and duration_seconds >= SOAK_DURATION_SECONDS
        and paper_identity_verified
        and reconnect_count == 1
        and unexpected_disconnect_count == 0
        and continuity_gap_count == 0
        and state_unchanged
        and rss_growth_bytes < SOAK_RSS_GROWTH_LIMIT_BYTES
        and maximum_shutdown_seconds < SOAK_SHUTDOWN_LIMIT_SECONDS
        and error_count == 0
    )
    return {
        "schema_version": 1,
        "provider": provider,
        "candidate": identity,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "duration_seconds": duration_seconds,
        "snapshot_interval_seconds": SOAK_SNAPSHOT_INTERVAL_SECONDS,
        "snapshots": snapshots,
        "paper_identity_verified": paper_identity_verified,
        "reconnect_count": reconnect_count,
        "unexpected_disconnect_count": unexpected_disconnect_count,
        "continuity_gap_count": continuity_gap_count,
        "initial_state_checksum": initial_checksum,
        "final_state_checksum": final_checksum,
        "final_reconciliation_exact": state_unchanged,
        "state_unchanged": state_unchanged,
        "rss_growth_bytes": rss_growth_bytes,
        "maximum_shutdown_seconds": maximum_shutdown_seconds,
        "error_count": error_count,
        "failed_stage": None if passed else stage,
        "failure_type": None if passed else failure_type,
        "passed": passed,
    }


EXERCISE_STEPS = frozenset(EXERCISE_STEP_SEQUENCE)
RESTART_STEPS = frozenset(RESTART_STEP_SEQUENCE)


def validate_provider_report(
    report: dict[str, Any], candidate_identity: dict[str, Any], provider: str, phase: str
) -> None:
    required = {
        "schema_version",
        "provider",
        "phase",
        "candidate",
        "started_at",
        "completed_at",
        "paper_identity_verified_before_submission",
        "steps_passed",
        "snapshots",
        "cleanup_passed",
        "failed_stage",
        "passed",
    }
    if set(report) != required or report.get("schema_version") != 1:
        raise PaperQualificationError("provider report schema is invalid")
    if report.get("provider") != provider or report.get("phase") != phase:
        raise PaperQualificationError("provider report identity is invalid")
    if _evidence_time(report["completed_at"]) < _evidence_time(report["started_at"]):
        raise PaperQualificationError("provider report timestamps are reversed")
    if report.get("candidate") != candidate_identity:
        raise PaperQualificationError("provider report targets a different candidate")
    expected_steps = EXERCISE_STEPS if phase == "exercise" else RESTART_STEPS
    steps = report.get("steps_passed", [])
    if (
        not isinstance(steps, list)
        or len(steps) != len(expected_steps)
        or set(steps) != expected_steps
    ):
        raise PaperQualificationError("provider report is missing mandatory paper operations")
    if (
        report.get("passed") is not True
        or report.get("cleanup_passed") is not True
        or report.get("paper_identity_verified_before_submission") is not True
        or report.get("failed_stage") is not None
    ):
        raise PaperQualificationError("provider report did not pass fail-closed qualification")
    snapshots = report.get("snapshots")
    expected_snapshots = {"initial", "reconnect", "final"} if phase == "exercise" else {"restart"}
    if not isinstance(snapshots, dict) or set(snapshots) != expected_snapshots:
        raise PaperQualificationError("provider report has incomplete reconciliation snapshots")
    snapshot_keys = {
        "positions_count",
        "pending_orders_count",
        "filtered_pending_orders_count",
        "position_snapshot_exact",
        "pending_order_snapshot_exact",
        "account_value_valid",
        "cash_valid",
    }
    for snapshot in snapshots.values():
        if not isinstance(snapshot, dict) or set(snapshot) != snapshot_keys:
            raise PaperQualificationError("provider report snapshot schema is invalid")
        counts = (
            snapshot["positions_count"],
            snapshot["pending_orders_count"],
            snapshot["filtered_pending_orders_count"],
        )
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts
        ):
            raise PaperQualificationError("provider report snapshot counts are invalid")
        if any(
            snapshot.get(name) is not True
            for name in (
                "position_snapshot_exact",
                "pending_order_snapshot_exact",
                "account_value_valid",
                "cash_valid",
            )
        ):
            raise PaperQualificationError("provider report contains an invalid snapshot")


def validate_provider_soak_report(
    report: dict[str, Any], candidate_identity: dict[str, Any], provider: str
) -> None:
    required = {
        "schema_version",
        "provider",
        "candidate",
        "started_at",
        "completed_at",
        "duration_seconds",
        "snapshot_interval_seconds",
        "snapshots",
        "paper_identity_verified",
        "reconnect_count",
        "unexpected_disconnect_count",
        "continuity_gap_count",
        "initial_state_checksum",
        "final_state_checksum",
        "final_reconciliation_exact",
        "state_unchanged",
        "rss_growth_bytes",
        "maximum_shutdown_seconds",
        "error_count",
        "failed_stage",
        "failure_type",
        "passed",
    }
    if set(report) != required or report.get("schema_version") != 1:
        raise PaperQualificationError("provider soak report schema is invalid")
    if report.get("provider") != provider or report.get("candidate") != candidate_identity:
        raise PaperQualificationError("provider soak report targets a different candidate")
    started_at = _evidence_time(report["started_at"])
    completed_at = _evidence_time(report["completed_at"])
    duration = report.get("duration_seconds")
    if completed_at < started_at:
        raise PaperQualificationError("provider soak report timestamps are reversed")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < SOAK_DURATION_SECONDS
        or (completed_at - started_at).total_seconds() < SOAK_DURATION_SECONDS
    ):
        raise PaperQualificationError("provider soak did not run for six hours")
    if report.get("snapshot_interval_seconds") != SOAK_SNAPSHOT_INTERVAL_SECONDS:
        raise PaperQualificationError("provider soak snapshot interval is invalid")
    snapshots = report.get("snapshots")
    expected_count = SOAK_DURATION_SECONDS // SOAK_SNAPSHOT_INTERVAL_SECONDS + 1
    snapshot_keys = {
        "elapsed_seconds",
        "rss_bytes",
        "positions_count",
        "pending_orders_count",
        "filtered_pending_orders_count",
        "position_snapshot_exact",
        "pending_order_snapshot_exact",
        "account_value_valid",
        "cash_valid",
        "connected",
    }
    if not isinstance(snapshots, list) or len(snapshots) < expected_count:
        raise PaperQualificationError("provider soak has incomplete snapshots")
    elapsed_values: list[float] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict) or set(snapshot) != snapshot_keys:
            raise PaperQualificationError("provider soak snapshot schema is invalid")
        elapsed = snapshot.get("elapsed_seconds")
        rss_bytes = snapshot.get("rss_bytes")
        counts = (
            snapshot.get("positions_count"),
            snapshot.get("pending_orders_count"),
            snapshot.get("filtered_pending_orders_count"),
        )
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or isinstance(rss_bytes, bool)
            or not isinstance(rss_bytes, int)
            or rss_bytes < 0
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 0
                for count in counts
            )
        ):
            raise PaperQualificationError("provider soak snapshot values are invalid")
        elapsed_values.append(float(elapsed))
        if any(
            snapshot.get(name) is not True
            for name in (
                "position_snapshot_exact",
                "pending_order_snapshot_exact",
                "account_value_valid",
                "cash_valid",
                "connected",
            )
        ):
            raise PaperQualificationError("provider soak contains an invalid snapshot")
    if (
        elapsed_values != sorted(elapsed_values)
        or elapsed_values[0] > SOAK_SNAPSHOT_INTERVAL_SECONDS
        or elapsed_values[-1] < SOAK_DURATION_SECONDS
        or any(
            current - previous > 2 * SOAK_SNAPSHOT_INTERVAL_SECONDS
            for previous, current in zip(elapsed_values, elapsed_values[1:])
        )
    ):
        raise PaperQualificationError("provider soak snapshot continuity is invalid")
    initial_checksum = report.get("initial_state_checksum")
    if (
        not isinstance(initial_checksum, str)
        or not HASH_PATTERN.fullmatch(initial_checksum)
        or report.get("final_state_checksum") != initial_checksum
    ):
        raise PaperQualificationError("provider soak state reconciliation is invalid")
    numeric_contract = {
        "reconnect_count": 1,
        "unexpected_disconnect_count": 0,
        "continuity_gap_count": 0,
        "error_count": 0,
    }
    if any(report.get(field) != value for field, value in numeric_contract.items()):
        raise PaperQualificationError("provider soak continuity contract did not pass")
    rss_growth = report.get("rss_growth_bytes")
    shutdown = report.get("maximum_shutdown_seconds")
    if (
        isinstance(rss_growth, bool)
        or not isinstance(rss_growth, int)
        or rss_growth < 0
        or rss_growth >= SOAK_RSS_GROWTH_LIMIT_BYTES
        or isinstance(shutdown, bool)
        or not isinstance(shutdown, (int, float))
        or not math.isfinite(shutdown)
        or shutdown < 0
        or shutdown >= SOAK_SHUTDOWN_LIMIT_SECONDS
    ):
        raise PaperQualificationError("provider soak resource or shutdown limit failed")
    if (
        report.get("paper_identity_verified") is not True
        or report.get("final_reconciliation_exact") is not True
        or report.get("state_unchanged") is not True
        or report.get("failed_stage") is not None
        or report.get("failure_type") is not None
        or report.get("passed") is not True
    ):
        raise PaperQualificationError("provider soak did not pass fail-closed qualification")


def assemble_bundle(
    candidate: dict[str, Any],
    reports: list[dict[str, Any]],
    soak_reports: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    identity = _candidate_identity(candidate)
    indexed = {(report.get("provider"), report.get("phase")): report for report in reports}
    if len(indexed) != len(reports) or set(indexed) != {
        (provider, phase) for provider in PROVIDERS for phase in PHASES
    }:
        raise PaperQualificationError("paper evidence requires both phases for both providers")
    for provider in PROVIDERS:
        for phase in PHASES:
            validate_provider_report(indexed[(provider, phase)], identity, provider, phase)
    indexed_soaks = {report.get("provider"): report for report in soak_reports}
    if len(indexed_soaks) != len(soak_reports) or set(indexed_soaks) != set(PROVIDERS):
        raise PaperQualificationError("paper evidence requires a six-hour soak for both providers")
    for provider in PROVIDERS:
        validate_provider_soak_report(indexed_soaks[provider], identity, provider)
    return {
        "schema_version": 1,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "candidate": identity,
        "providers": {
            provider: {phase: indexed[(provider, phase)] for phase in PHASES}
            for provider in PROVIDERS
        },
        "soaks": indexed_soaks,
        "redacted": True,
        "passed": True,
    }


def validate_bundle(bundle: dict[str, Any], *, expected_commit: str | None = None) -> None:
    if (
        set(bundle)
        != {
            "schema_version",
            "generated_at",
            "candidate",
            "providers",
            "soaks",
            "redacted",
            "passed",
        }
        or bundle.get("schema_version") != 1
    ):
        raise PaperQualificationError("paper evidence bundle schema is invalid")
    candidate = bundle.get("candidate")
    if not isinstance(candidate, dict) or set(candidate) != {
        "commit",
        "qualification_run_id",
        "version",
        "wheel_sha256",
        "sdist_sha256",
    }:
        raise PaperQualificationError("paper evidence candidate identity is invalid")
    if (
        not COMMIT_PATTERN.fullmatch(str(candidate.get("commit", "")))
        or not HASH_PATTERN.fullmatch(str(candidate.get("wheel_sha256", "")))
        or not HASH_PATTERN.fullmatch(str(candidate.get("sdist_sha256", "")))
    ):
        raise PaperQualificationError("paper evidence candidate hashes are invalid")
    if expected_commit is not None and candidate["commit"] != expected_commit:
        raise PaperQualificationError("paper evidence targets a different commit")
    providers = bundle.get("providers")
    if not isinstance(providers, dict) or set(providers) != set(PROVIDERS):
        raise PaperQualificationError("paper evidence does not cover both providers")
    completed_at: list[datetime] = []
    for provider in PROVIDERS:
        phases = providers[provider]
        if not isinstance(phases, dict) or set(phases) != set(PHASES):
            raise PaperQualificationError("paper evidence has incomplete provider phases")
        for phase in PHASES:
            validate_provider_report(phases[phase], candidate, provider, phase)
            completed_at.append(_evidence_time(phases[phase]["completed_at"]))
    soaks = bundle.get("soaks")
    if not isinstance(soaks, dict) or set(soaks) != set(PROVIDERS):
        raise PaperQualificationError("paper evidence has incomplete provider soaks")
    for provider in PROVIDERS:
        validate_provider_soak_report(soaks[provider], candidate, provider)
        completed_at.append(_evidence_time(soaks[provider]["completed_at"]))
    if _evidence_time(bundle["generated_at"]) < max(completed_at):
        raise PaperQualificationError("paper evidence bundle predates a provider report")
    if bundle.get("redacted") is not True or bundle.get("passed") is not True:
        raise PaperQualificationError("paper evidence is not passing redacted evidence")


def _candidate_command(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise PaperQualificationError("GITHUB_TOKEN is required to verify qualification run")
    run = _fetch_json(
        f"{GITHUB_API}/repos/{args.repository}/actions/runs/{args.qualification_run_id}", token
    )
    checkout_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=args.checkout_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if checkout_sha != args.candidate_sha:
        raise PaperQualificationError("checked-out harness differs from the candidate commit")
    tracked_diff = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--"], cwd=args.checkout_root, check=False
    )
    if tracked_diff.returncode != 0:
        raise PaperQualificationError("tracked candidate files changed before paper qualification")
    manifest = build_candidate_manifest(
        artifacts_directory=args.artifacts_dir,
        candidate_sha=args.candidate_sha,
        qualification_run_id=args.qualification_run_id,
        repository=args.repository,
        run_record=run,
    )
    _write_json(args.output, manifest)
    print(f"paper candidate: PASS commit={args.candidate_sha} wheel={manifest['wheel']['sha256']}")
    return 0


def _provider_command(args: argparse.Namespace) -> int:
    candidate = _load_json(args.candidate)
    report = asyncio.run(
        run_provider_phase(
            provider=args.provider,
            phase=args.phase,
            candidate=candidate,
            checkout_root=args.checkout_root,
            state_directory=args.state_directory,
            tag_seed=args.tag_seed,
        )
    )
    _write_json(args.output, report)
    print(f"paper {args.provider} {args.phase}: {'PASS' if report['passed'] else 'FAIL'}")
    return int(not report["passed"])


def _soak_command(args: argparse.Namespace) -> int:
    candidate = _load_json(args.candidate)
    report = asyncio.run(
        run_provider_soak(
            provider=args.provider,
            candidate=candidate,
            checkout_root=args.checkout_root,
        )
    )
    _write_json(args.output, report)
    print(f"paper {args.provider} soak: {'PASS' if report['passed'] else 'FAIL'}")
    return int(not report["passed"])


def _assemble_command(args: argparse.Namespace) -> int:
    candidate = _load_json(args.candidate)
    reports = [_load_json(path) for path in args.report]
    soak_reports = [_load_json(path) for path in args.soak_report]
    bundle = assemble_bundle(candidate, reports, soak_reports)
    _write_json(args.output, bundle)
    print(
        f"paper qualification: PASS commit={bundle['candidate']['commit']} "
        f"wheel={bundle['candidate']['wheel_sha256']}"
    )
    return 0


def _validate_command(args: argparse.Namespace) -> int:
    validate_bundle(_load_json(args.bundle), expected_commit=args.expected_commit)
    print("paper evidence bundle: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("--artifacts-dir", type=Path, required=True)
    candidate.add_argument("--candidate-sha", required=True)
    candidate.add_argument("--qualification-run-id", type=int, required=True)
    candidate.add_argument("--repository", required=True)
    candidate.add_argument("--checkout-root", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.set_defaults(handler=_candidate_command)

    provider = subparsers.add_parser("provider")
    provider.add_argument("--candidate", type=Path, required=True)
    provider.add_argument("--provider", choices=PROVIDERS, required=True)
    provider.add_argument("--phase", choices=PHASES, required=True)
    provider.add_argument("--checkout-root", type=Path, required=True)
    provider.add_argument("--state-directory", type=Path, required=True)
    provider.add_argument("--tag-seed", required=True)
    provider.add_argument("--output", type=Path, required=True)
    provider.set_defaults(handler=_provider_command)

    soak = subparsers.add_parser("soak")
    soak.add_argument("--candidate", type=Path, required=True)
    soak.add_argument("--provider", choices=PROVIDERS, required=True)
    soak.add_argument("--checkout-root", type=Path, required=True)
    soak.add_argument("--output", type=Path, required=True)
    soak.set_defaults(handler=_soak_command)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--candidate", type=Path, required=True)
    assemble.add_argument("--report", action="append", type=Path, required=True)
    assemble.add_argument("--soak-report", action="append", type=Path, required=True)
    assemble.add_argument("--output", type=Path, required=True)
    assemble.set_defaults(handler=_assemble_command)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--expected-commit")
    validate.set_defaults(handler=_validate_command)

    args = parser.parse_args()
    try:
        return int(args.handler(args))
    except PaperQualificationError as error:
        print(f"paper qualification: FAIL ({error})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
