"""Record unsupported Python 3.15 source and public-calendar diagnostic probes."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import sys
import tomllib
import traceback
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    output.write_text(rendered)
    print(rendered, flush=True)


def probe(kind: str, output: Path) -> int:
    report: dict[str, Any] = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "probe": kind,
        "scope": "Unsupported diagnostic only; no normal Live installation or Python 3.15 qualification.",
        "packages": {
            distribution.metadata["Name"]: distribution.version
            for distribution in importlib.metadata.distributions()
            if distribution.metadata["Name"]
        },
        "passed": False,
    }
    try:
        if sys.version_info[:2] != (3, 15) or sys.version_info.releaselevel != "final":
            raise RuntimeError("the canary requires the actual final Python 3.15 interpreter")
        if kind == "source":
            sys.path.insert(0, str(ROOT / "src"))
            import ml4t.live

            location = Path(ml4t.live.__file__).resolve()
            if not location.is_relative_to(ROOT / "src"):
                raise RuntimeError("diagnostic source probe imported another Live checkout")
            report["source_import"] = str(location)
        else:
            import ml4t.backtest
            import ml4t.specs
            from ml4t.backtest.calendar import get_schedule

            report["imports"] = {
                "ml4t.backtest": ml4t.backtest.__file__,
                "ml4t.specs": ml4t.specs.__file__,
            }
            schedule = get_schedule("HKEX", "2025-07-07", "2025-07-07", include_breaks=True)
            rows = schedule.to_dicts()
            report["schedule"] = rows
            if len(rows) != 1 or any(
                rows[0][field] != expected
                for field, expected in {
                    "session_date": date(2025, 7, 7),
                    "market_open": datetime(2025, 7, 7, 1, 30, tzinfo=UTC),
                    "break_start": datetime(2025, 7, 7, 4, tzinfo=UTC),
                    "break_end": datetime(2025, 7, 7, 5, tzinfo=UTC),
                    "market_close": datetime(2025, 7, 7, 8, tzinfo=UTC),
                }.items()
            ):
                raise RuntimeError("public Hong Kong schedule differs from its declared session")
        report["passed"] = True
    except Exception as error:
        report["failure_type"] = type(error).__name__
        report["failure"] = str(error)
        report["traceback"] = traceback.format_exc()
        traceback.print_exc()
    write_report(output, report)
    return int(not report["passed"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("requirements", "source", "calendar"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "requirements":
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(project["dependencies"]) + "\n")
        print("Exported every mandatory Live runtime requirement; Live itself is not installed.")
        return 0
    return probe(args.mode, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
