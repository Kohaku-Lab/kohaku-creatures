"""Low-level helpers for ``SkillManageTool`` — kept separate to respect
per-file size limits.

Provides:
- :func:`iso_now` — ISO-8601 UTC timestamp with ``Z`` suffix.
- :func:`serialize_skill` — ``---\\n<yaml>\\n---\\n\\n<body>\\n`` writer
  with a stable, agentskills.io-friendly field order.
- :func:`atomic_write` — write-tmp-then-``os.replace`` for durability.
"""

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

_ORDERED_KEYS: tuple[str, ...] = (
    "name",
    "description",
    "license",
    "compatibility",
    "created_at",
    "updated_at",
)


def iso_now() -> str:
    """ISO-8601 UTC timestamp (second precision, trailing ``Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serialize_skill(metadata: dict[str, Any], body: str) -> str:
    """Compose a SKILL.md document with a stable frontmatter field order."""
    ordered: dict[str, Any] = {}
    for key in _ORDERED_KEYS:
        if key in metadata:
            ordered[key] = metadata[key]
    for key, value in metadata.items():
        if key not in ordered:
            ordered[key] = value
    yaml_text = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True).strip()
    body_text = body.rstrip() + "\n" if body.strip() else ""
    return f"---\n{yaml_text}\n---\n\n{body_text}"


def atomic_write(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text`` (tmp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
