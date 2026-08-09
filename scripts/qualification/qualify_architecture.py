"""Qualify import boundaries, protocol conformance, and installed public typing."""

from __future__ import annotations

import argparse
import ast
import hashlib
import inspect
import json
import os
import subprocess
import tempfile
import tokenize
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src" / "ml4t" / "live"
BUILD_CONSTRAINTS = REPOSITORY_ROOT / "build-constraints.txt"
SUPPORTED_PYTHONS = ("3.12", "3.13", "3.14")
FORBIDDEN_SUPPRESSIONS = (
    "type: ignore",
    "# noqa",
    "pragma: no cover",
    "pyright: ignore",
    "ty: ignore",
)
EXTERNAL_CONSUMER = """\
import asyncio

from ib_async import IB
from ml4t.live import (
    AlpacaBroker,
    AlpacaDataFeed,
    AsyncBrokerProtocol,
    BarAggregator,
    BrokerProtocol,
    DataFeedProtocol,
    IBBroker,
    IBDataFeed,
    LiveEngine,
    LiveRiskConfig,
    OKXFundingFeed,
    SafeBroker,
    ThreadSafeBrokerWrapper,
)
from ml4t.backtest import Strategy


def brokers() -> tuple[AsyncBrokerProtocol, AsyncBrokerProtocol]:
    return AlpacaBroker("key", "secret"), IBBroker()


def safe_broker(broker: AsyncBrokerProtocol) -> AsyncBrokerProtocol:
    return SafeBroker(broker, LiveRiskConfig(execution_mode="shadow"))


def sync_broker(
    broker: AsyncBrokerProtocol,
    loop: asyncio.AbstractEventLoop,
) -> BrokerProtocol:
    return ThreadSafeBrokerWrapper(broker, loop)


def feeds() -> tuple[DataFeedProtocol, DataFeedProtocol, DataFeedProtocol]:
    okx: DataFeedProtocol = OKXFundingFeed(["BTC-USDT-SWAP"])
    alpaca: DataFeedProtocol = AlpacaDataFeed(
        "key", "secret", ["SPY"], experimental=True
    )
    ib: DataFeedProtocol = IBDataFeed(IB(), ["SPY"], experimental=True)
    aggregated: DataFeedProtocol = BarAggregator(okx)
    return alpaca, ib, aggregated


def engine(
    strategy: Strategy,
    broker: AsyncBrokerProtocol,
    feed: DataFeedProtocol,
) -> LiveEngine:
    return LiveEngine(strategy, broker, feed)
"""
INSTALLED_RUNTIME = """\
from pathlib import Path

from ib_async import IB
from ml4t.live import (
    AlpacaBroker,
    AlpacaDataFeed,
    AsyncBrokerProtocol,
    BarAggregator,
    DataFeedProtocol,
    IBBroker,
    IBDataFeed,
    LiveRiskConfig,
    OKXFundingFeed,
    SafeBroker,
)

alpaca = AlpacaBroker("key", "secret")
ib = IBBroker()
okx_feed = OKXFundingFeed(["BTC-USDT-SWAP"])
feeds = (
    AlpacaDataFeed("key", "secret", ["SPY"], experimental=True),
    IBDataFeed(IB(), ["SPY"], experimental=True),
    okx_feed,
    BarAggregator(okx_feed),
)
safe = SafeBroker(
    alpaca,
    LiveRiskConfig(execution_mode="shadow", state_file=str(Path.cwd() / "state.json")),
)
try:
    assert all(isinstance(broker, AsyncBrokerProtocol) for broker in (alpaca, ib, safe))
    assert all(isinstance(feed, DataFeedProtocol) for feed in feeds)
finally:
    safe.close_persistence()
"""


def run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command[:4])}\n{detail}"
        )
    return result.stdout


def source_modules(root: Path = SOURCE_ROOT) -> dict[str, Path]:
    modules = {}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root).with_suffix("")
        parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
        module = ".".join(("ml4t", "live", *parts))
        modules[module] = path
    return modules


def import_graph(root: Path = SOURCE_ROOT) -> dict[str, set[str]]:
    modules = source_modules(root)
    graph = {module: set() for module in modules}
    for module, path in modules.items():
        tree = ast.parse(path.read_text(), filename=str(path))
        package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package[: len(package) - node.level + 1]
                    targets = [".".join((*prefix, *(node.module or "").split("."))).rstrip(".")]
                else:
                    targets = [node.module or ""]
            else:
                continue
            for target in targets:
                if target in modules and target != module:
                    graph[module].add(target)
    return graph


def import_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: set[tuple[str, ...]] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in path:
            cycle = path[path.index(node) :]
            rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
            cycles.add(min(rotations))
            return
        for dependency in graph[node]:
            visit(dependency, [*path, node])

    for module in graph:
        visit(module, [])
    return [list(cycle) for cycle in sorted(cycles)]


def suppression_inventory(root: Path = REPOSITORY_ROOT) -> list[dict[str, object]]:
    records = []
    roots = (
        [root / name for name in ("src", "tests", "scripts", "examples")]
        if root == REPOSITORY_ROOT
        else [root]
    )
    for path in sorted(path for base in roots for path in base.rglob("*.py")):
        with tokenize.open(path) as stream:
            comments = [
                token
                for token in tokenize.generate_tokens(stream.readline)
                if token.type == tokenize.COMMENT
            ]
        for comment in comments:
            for marker in FORBIDDEN_SUPPRESSIONS:
                if marker in comment.string:
                    try:
                        display_path = str(path.relative_to(REPOSITORY_ROOT))
                    except ValueError:
                        display_path = str(path)
                    records.append(
                        {
                            "path": display_path,
                            "line": comment.start[0],
                            "marker": marker,
                        }
                    )
    return records


def public_annotation_gaps(exports: dict[str, object] | None = None) -> list[str]:
    if exports is None:
        import ml4t.live as live

        exports = {name: getattr(live, name) for name in live.__all__}
    gaps = []
    for export_name, value in sorted(exports.items()):
        callables: list[tuple[str, Any]] = []
        if inspect.isfunction(value):
            callables.append((export_name, value))
        elif inspect.isclass(value):
            for member_name, raw in value.__dict__.items():
                if member_name.startswith("_") and member_name != "__init__":
                    continue
                if isinstance(raw, property):
                    member = raw.fget
                elif isinstance(raw, classmethod | staticmethod):
                    member = raw.__func__
                else:
                    member = raw
                if inspect.isfunction(member) and member.__module__.startswith("ml4t.live"):
                    callables.append((f"{export_name}.{member_name}", member))
        for qualified_name, function in callables:
            signature = inspect.signature(function)
            for parameter in signature.parameters.values():
                if parameter.name in {"self", "cls"}:
                    continue
                if parameter.annotation is inspect.Parameter.empty:
                    gaps.append(f"{qualified_name}:{parameter.name}")
            if signature.return_annotation is inspect.Signature.empty:
                gaps.append(f"{qualified_name}:return")
    return gaps


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def qualify(output: Path | None = None) -> dict[str, object]:
    graph = import_graph()
    cycles = import_cycles(graph)
    suppressions = suppression_inventory()
    annotation_gaps = public_annotation_gaps()
    if cycles:
        raise RuntimeError(f"internal import cycles found: {cycles}")
    if suppressions:
        raise RuntimeError(f"source suppressions require an owner: {suppressions}")
    if annotation_gaps:
        raise RuntimeError(f"public annotations are incomplete: {annotation_gaps}")

    with tempfile.TemporaryDirectory(prefix="ml4t-live-architecture-") as temporary:
        root = Path(temporary)
        dist = root / "dist"
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["SETUPTOOLS_SCM_PRETEND_VERSION"] = "0.1.0b4"
        run(
            [
                "uv",
                "build",
                "--wheel",
                "--out-dir",
                str(dist),
                "--build-constraints",
                str(BUILD_CONSTRAINTS),
            ],
            cwd=REPOSITORY_ROOT,
            environment=environment,
        )
        wheel = next(dist.glob("*.whl"))
        consumer = root / "consumer.py"
        consumer.write_text(EXTERNAL_CONSUMER)
        installed_runtime = root / "installed_runtime.py"
        installed_runtime.write_text(INSTALLED_RUNTIME)
        profiles = []
        for version in SUPPORTED_PYTHONS:
            venv = root / f"python-{version}"
            run(["uv", "venv", "--python", version, str(venv)], cwd=root)
            python = venv / "bin" / "python"
            run(
                ["uv", "pip", "install", "--python", str(python), str(wheel), "ty==0.0.69"],
                cwd=root,
            )
            run(
                [str(venv / "bin" / "ty"), "check", "--python", str(python), str(consumer)],
                cwd=root,
                environment=environment,
            )
            run(
                [str(python), "-I", str(installed_runtime)],
                cwd=root,
                environment=environment,
            )
            profiles.append({"python": version, "passed": True})

        report: dict[str, object] = {
            "schema_version": 1,
            "source_revision": run(["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT).strip(),
            "wheel": wheel.name,
            "wheel_sha256": sha256(wheel),
            "profiles": profiles,
            "source_suppressions": suppressions,
            "public_annotation_gaps": annotation_gaps,
            "internal_import_cycles": cycles,
            "module_lines": {
                module: len(path.read_text().splitlines())
                for module, path in sorted(source_modules().items())
            },
            "internal_imports": {
                module: sorted(dependencies) for module, dependencies in sorted(graph.items())
            },
        }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(json.dumps(qualify(args.output), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
