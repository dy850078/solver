"""UI JavaScript checks, driven from pytest so `make test` (and the Stop
hook) cover the browser code too.

The UI ships hand-written, dependency-free JS (CLAUDE.md: no third-party
JS, no CDN). Its DOM-free modules are unit-tested under Node's built-in
runner (tests/js/*.test.mjs — no npm install). This file

  1. runs that Node suite, and
  2. cross-checks the PM Demand workbook with judges that share no code
     with the UI: Python's zipfile + ElementTree read the bytes xlsx.js
     wrote, and the Pydantic model compares the request demand-xlsx.js
     reads back. A JS-only round trip can agree with itself while being
     wrong about the format; these cannot.

Everything here needs `node` (>= 18). Without it the tests SKIP — the
Python suite must stay runnable on a machine with no Node. CI must
therefore run with SOLVER_REQUIRE_NODE=1, which turns the skip into a
failure so a runner that lost Node cannot go quietly green.
"""

import json
import os
import pathlib
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import pytest

from app.bm_demand import BmDemandPlanRequest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS_TESTS = ROOT / "tests" / "js"
DEMAND_EXAMPLES = sorted((ROOT / "examples" / "bm_demand").glob("*.json"))
NODE = shutil.which("node")
MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@pytest.fixture(autouse=True)
def _need_node():
    if NODE is None:
        if os.environ.get("SOLVER_REQUIRE_NODE"):
            pytest.fail("node is required (SOLVER_REQUIRE_NODE is set) but not on PATH")
        pytest.skip("node not installed — UI JS tests skipped")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([NODE, *args], cwd=ROOT, capture_output=True,
                          text=True, timeout=120)


def test_node_unit_suite():
    files = sorted(str(p) for p in JS_TESTS.glob("*.test.mjs"))
    assert files, "no tests/js/*.test.mjs files found"
    proc = _run("--test", *files)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]


def _model(data: dict) -> dict:
    """The request as the server understands it. The demand page strips
    `_name` / `_description` before POST. `build_order` is compared with
    [] folded into None: the importer writes an empty Settings cell as [],
    and compile_bm_demands treats an empty order exactly like an absent one
    (nothing to reorder) — the only field where the two spellings differ."""
    dumped = BmDemandPlanRequest.model_validate(
        {k: v for k, v in data.items() if not k.startswith("_")}).model_dump()
    dumped["build_order"] = dumped["build_order"] or None
    return dumped


@pytest.mark.parametrize("example", DEMAND_EXAMPLES, ids=lambda p: p.stem)
def test_demand_workbook_cross_check(example, tmp_path):
    xlsx, back = tmp_path / "demand.xlsx", tmp_path / "back.json"
    proc = _run(str(JS_TESTS / "demand-workbook-cli.mjs"),
                str(example), str(xlsx), str(back))
    assert proc.returncode == 0, proc.stderr

    # (a) The server reads the re-imported request exactly as the original.
    original = json.loads(example.read_text())
    assert _model(json.loads(back.read_text())) == _model(original)

    # (b) The bytes are a well-formed workbook to an independent reader:
    # every CRC checks out, every part parses, and the Demands sheet holds
    # one row per demand plus the header.
    with zipfile.ZipFile(xlsx) as zf:
        assert zf.testzip() is None, "CRC mismatch in a ZIP entry"
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "xl/workbook.xml",
                "xl/worksheets/sheet1.xml"} <= names
        for name in names:
            if name.endswith((".xml", ".rels")):
                ET.fromstring(zf.read(name))
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        sheet_names = [s.get("name") for s in workbook.iter(MAIN_NS + "sheet")]
        assert sheet_names == ["Demands", "VM specs", "Machine models",
                               "Groups", "Settings"]
        rows = list(ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
                    .iter(MAIN_NS + "row"))
        assert len(rows) == len(original["demands"]) + 1
