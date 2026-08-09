"""Capture the installed and source-visible ml4t-live support surface."""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import pkgutil
import textwrap
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from importlib import metadata
from pathlib import Path
from typing import Any

import ml4t.live as live

EXPERIMENTAL_EXPORTS = {
    "CryptoFeed",
    "DataBentoFeed",
    "ExperimentalFeedError",
    "ExperimentalFeedWarning",
}


def source_modules(source_root: Path) -> dict[str, Path]:
    package_root = source_root / "ml4t" / "live"
    if not package_root.is_dir():
        raise ValueError(f"source root does not contain ml4t/live: {source_root}")
    modules: dict[str, Path] = {}
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "_version.py" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source_root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


def assigned_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def source_public_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    }
    for node in tree.body:
        name = assigned_name(node)
        if name and name.isupper() and not name.startswith("_"):
            names.add(name)
    return names


def stable_default(value: Any) -> str:
    if isinstance(value, set | frozenset):
        return repr(sorted(value))
    return repr(value)


def dataclass_field_records(owner: type) -> list[dict[str, str]]:
    records = []
    for field in fields(owner):
        if field.default is not MISSING:
            default = stable_default(field.default)
        elif field.default_factory is not MISSING:
            factory_name = getattr(
                field.default_factory, "__name__", type(field.default_factory).__name__
            )
            default = f"<factory:{factory_name}>"
        else:
            default = "<required>"
        records.append({"name": field.name, "type": str(field.type), "default": default})
    return records


def signature_text(value: Any) -> str | None:
    try:
        return str(inspect.signature(value)).replace("typing.", "")
    except (TypeError, ValueError):
        return None


def method_records(owner: type) -> list[dict[str, str | None]]:
    records = []
    for name, raw in sorted(vars(owner).items()):
        if name.startswith("_"):
            continue
        kind = None
        value = raw
        if isinstance(raw, classmethod | staticmethod):
            kind = type(raw).__name__
            value = raw.__func__
        elif isinstance(raw, property):
            kind = "property"
            value = raw.fget
        elif callable(raw):
            kind = "method"
        if kind:
            records.append({"name": name, "kind": kind, "signature": signature_text(value)})
    return records


def symbol_kind(value: Any) -> str:
    if inspect.isclass(value):
        if issubclass(value, BaseException):
            return "exception"
        if issubclass(value, Enum):
            return "enum"
        if getattr(value, "_is_protocol", False):
            return "protocol"
        if is_dataclass(value):
            return "dataclass"
        return "class"
    if inspect.isfunction(value):
        return "function"
    return "constant"


def classify(module_name: str, name: str, root_exports: set[str]) -> str:
    if name in EXPERIMENTAL_EXPORTS:
        return "experimental"
    if module_name == "ml4t.live" or name in root_exports:
        return "stable"
    return "internal"


def symbol_record(
    module_name: str, name: str, value: Any, root_exports: set[str]
) -> dict[str, Any]:
    kind = symbol_kind(value)
    record: dict[str, Any] = {
        "module": module_name,
        "name": name,
        "classification": classify(module_name, name, root_exports),
        "kind": kind,
        "defined_in": getattr(value, "__module__", module_name),
    }
    if callable(value):
        record["signature"] = signature_text(value)
    if inspect.isclass(value):
        record["bases"] = [f"{base.__module__}.{base.__qualname__}" for base in value.__bases__]
        record["methods"] = method_records(value)
        if is_dataclass(value):
            record["dataclass_fields"] = dataclass_field_records(value)
        if issubclass(value, Enum):
            record["enum_members"] = [
                {"name": member.name, "value": stable_default(member.value)} for member in value
            ]
    return record


def cli_surface() -> dict[str, Any]:
    try:
        cli = importlib.import_module("ml4t.live.cli.main")
    except ModuleNotFoundError:
        return {}
    parser = cli.build_parser()
    commands: dict[str, Any] = {}
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        for name, command_parser in sorted(choices.items()):
            arguments = []
            for argument in command_parser._actions:
                if argument.dest == "help":
                    continue
                arguments.append(
                    {
                        "dest": argument.dest,
                        "options": list(argument.option_strings),
                        "required": argument.required,
                        "choices": sorted(argument.choices) if argument.choices else None,
                        "default": stable_default(argument.default),
                        "type": getattr(argument.type, "__name__", None),
                    }
                )
            commands[name] = {"classification": "stable", "arguments": arguments}
    return commands


def entry_point_surface() -> dict[str, str]:
    distribution = metadata.distribution("ml4t-live")
    return {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }


def returned_mapping_keys(value: Any) -> list[str]:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(value)))
    except (OSError, TypeError):
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = [key.value for key in node.value.keys if isinstance(key, ast.Constant)]
            if keys:
                return [str(key) for key in keys]
    return []


def persisted_schema_surface() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        safety = importlib.import_module("ml4t.live.safety")
    except ModuleNotFoundError:
        safety = None
    try:
        persistence = importlib.import_module("ml4t.live.persistence")
    except ModuleNotFoundError:
        persistence = None
    try:
        runtime = importlib.import_module("ml4t.live.runtime")
    except ModuleNotFoundError:
        runtime = None

    risk_state = getattr(safety, "RiskState", None)
    if risk_state is not None and is_dataclass(risk_state):
        result["risk_state"] = {
            "version": getattr(persistence, "STATE_SCHEMA_VERSION", "unversioned"),
            "envelope_fields": (
                ["schema_version", "generation", "payload", "checksum"]
                if persistence is not None
                else []
            ),
            "fields": [field.name for field in fields(risk_state)],
        }

    journal = getattr(persistence, "SecureAuditJournal", None)
    if journal is not None:
        result["audit_journal"] = {
            "version": 1,
            "record_fields": [
                "schema_version",
                "sequence",
                "timestamp",
                "event",
                "payload",
                "previous_hash",
                "entry_hash",
            ],
            "head_fields": ["schema_version", "sequence", "head_hash", "checksum"],
        }

    runtime_owner = getattr(runtime, "LiveStrategyRuntime", None)
    to_state = getattr(runtime_owner, "to_state", None)
    if to_state is not None:
        result["portable_strategy_state"] = {
            "version": 1,
            "fields": returned_mapping_keys(to_state),
        }
    return result


def capture_surface(source_root: Path) -> dict[str, Any]:
    modules = source_modules(source_root)
    root_exports = set(live.__all__)
    symbols: dict[tuple[str, str], dict[str, Any]] = {}
    mismatches = []
    module_exports: dict[str, list[str]] = {}

    for module_name, path in modules.items():
        module = importlib.import_module(module_name)
        source_names = source_public_names(path)
        missing = sorted(name for name in source_names if not hasattr(module, name))
        if missing:
            mismatches.append({"module": module_name, "missing_at_runtime": missing})
        exports = sorted(getattr(module, "__all__", ()))
        if exports:
            module_exports[module_name] = exports
        for name in sorted(source_names | set(exports)):
            if hasattr(module, name):
                symbols[(module_name, name)] = symbol_record(
                    module_name, name, getattr(module, name), root_exports
                )

    for name in sorted(root_exports | {"__version__"}):
        symbols[("ml4t.live", name)] = symbol_record(
            "ml4t.live", name, getattr(live, name), root_exports
        )

    package_modules = sorted(
        module.name
        for module in pkgutil.walk_packages(live.__path__, prefix="ml4t.live.")
        if "._" not in module.name
    )
    missing_source_modules = sorted(set(package_modules) - set(modules))
    if missing_source_modules:
        mismatches.append({"runtime_modules_without_source": missing_source_modules})

    return {
        "schema_version": 1,
        "distribution": {
            "name": "ml4t-live",
            "version": metadata.version("ml4t-live"),
        },
        "root_exports": sorted(root_exports),
        "module_exports": module_exports,
        "symbols": [symbols[key] for key in sorted(symbols)],
        "cli": cli_surface(),
        "entry_points": entry_point_surface(),
        "persisted_schemas": persisted_schema_surface(),
        "classification_rules": {
            "root_exports": "stable unless named in experimental_exports",
            "module_exports": "same classification as the root object when root-exported",
            "source_definitions_not_exported": "internal",
            "experimental_exports": sorted(EXPERIMENTAL_EXPORTS),
        },
        "source_runtime_mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(capture_surface(args.source_root), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(result)
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
