"""Examples API — list and fetch sample request JSON from the examples/ directory.

Recursively scans examples/ (including subdirectories like examples/splitter/
and examples/placement/) so users can organize fixtures however they like.
Endpoint hint is inferred from the parent directory name first
(splitter → split-and-solve, placement → solve), falling back to the
"split_" filename prefix for files at the top level.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/examples", tags=["examples"])

_EXAMPLES_DIR = (Path(__file__).parent.parent / "examples").resolve()
_SEGMENT_PATTERN = re.compile(r"^[\w\-.]+$")

# Directory name → endpoint hint
_DIR_HINTS = {
    "splitter": "split-and-solve",
    "split": "split-and-solve",
    "placement": "solve",
    "solve": "solve",
    "capacity": "capacity-plan",
    "rollout": "rollout",
}


class ExampleEntry(BaseModel):
    name: str           # relative POSIX path, e.g. "splitter/basic.json"
    endpoint_hint: str  # "solve" or "split-and-solve"


def _hint_for(rel_path: Path) -> str:
    # Directory-based hint takes priority
    parts = rel_path.parts
    if len(parts) > 1:
        dir_hint = _DIR_HINTS.get(parts[0])
        if dir_hint:
            return dir_hint
    # Fallback: legacy "split_" filename prefix
    return "split-and-solve" if rel_path.name.startswith("split_") else "solve"


def _resolve_example(name: str) -> Path:
    """Validate user-supplied example name (possibly multi-segment) and
    return its resolved Path under _EXAMPLES_DIR.
    """
    if not name or name.startswith("/") or "\\" in name:
        raise HTTPException(status_code=400, detail="invalid example name")
    segments = name.split("/")
    for s in segments:
        if not s or s in (".", "..") or not _SEGMENT_PATTERN.match(s):
            raise HTTPException(status_code=400, detail="invalid example name")
    if not segments[-1].endswith(".json"):
        raise HTTPException(status_code=400, detail="invalid example name")
    target = (_EXAMPLES_DIR / name).resolve()
    try:
        target.relative_to(_EXAMPLES_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid example path")
    return target


@router.get("", response_model=list[ExampleEntry])
def list_examples() -> list[ExampleEntry]:
    if not _EXAMPLES_DIR.is_dir():
        return []
    entries: list[ExampleEntry] = []
    for path in sorted(_EXAMPLES_DIR.rglob("*.json")):
        rel = path.relative_to(_EXAMPLES_DIR)
        entries.append(ExampleEntry(
            name=rel.as_posix(),
            endpoint_hint=_hint_for(rel),
        ))
    return entries


@router.get("/{name:path}")
def get_example(name: str) -> dict:
    target = _resolve_example(name)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="example not found")
    with target.open() as f:
        return json.load(f)
