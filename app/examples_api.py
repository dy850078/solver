"""Examples API — list and fetch sample request JSON from the examples/ directory.

Served at /api/examples and /api/examples/{name}. Used by the web UI so users
can load known-good request payloads with one click.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/examples", tags=["examples"])

_EXAMPLES_DIR = (Path(__file__).parent.parent / "examples").resolve()
_NAME_PATTERN = re.compile(r"^[\w\-.]+\.json$")


class ExampleEntry(BaseModel):
    name: str
    endpoint_hint: str  # "solve" or "split-and-solve"


def _hint_for(name: str) -> str:
    return "split-and-solve" if name.startswith("split_") else "solve"


@router.get("", response_model=list[ExampleEntry])
def list_examples() -> list[ExampleEntry]:
    if not _EXAMPLES_DIR.is_dir():
        return []
    return [
        ExampleEntry(name=p.name, endpoint_hint=_hint_for(p.name))
        for p in sorted(_EXAMPLES_DIR.glob("*.json"))
    ]


@router.get("/{name}")
def get_example(name: str) -> dict:
    if not _NAME_PATTERN.match(name):
        raise HTTPException(status_code=400, detail="invalid example name")
    target = (_EXAMPLES_DIR / name).resolve()
    try:
        target.relative_to(_EXAMPLES_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid example path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="example not found")
    with target.open() as f:
        return json.load(f)
