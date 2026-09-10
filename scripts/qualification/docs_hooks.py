"""MkDocs hooks for recording the immutable documentation release identity."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def on_post_build(config: dict[str, Any], **_: object) -> None:
    """Write the candidate identity beside the rendered documentation."""
    extra = config["extra"]
    identity = {
        "library": extra["ml4t_library"],
        "version": extra["ml4t_version"],
        "commit": extra["ml4t_commit"],
    }
    site_dir = Path(config["site_dir"])
    (site_dir / "release.json").write_text(
        json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8"
    )
