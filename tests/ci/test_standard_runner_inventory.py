"""Standard-runner plans retain complete inventories and reject empty discovery."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from scripts import run_tests_parallel as runner
from tests.ci import workflow_steps

ROOT = Path(__file__).resolve().parents[2]


def workflow(name):
    return YAML(typ="safe").load((ROOT / ".github/workflows" / name).read_text())


@pytest.mark.parametrize("filename,job,root,key", [
    ("tests.yml", "e2e-upgrade-plan", "tests/e2e/core/upgrade", "shards"),
    ("windows-install-update-e2e.yml", "plan", "tests/e2e/core/windows_update", "files"),
])
def test_plans_discover_nested_files_once_and_refuse_empty(tmp_path, filename, job, root, key):
    step = next(s for s in workflow(filename)["jobs"][job]["steps"] if s.get("id") == "plan")
    base = tmp_path / root
    base.mkdir(parents=True)
    with pytest.raises(AssertionError):
        workflow_steps.outputs(step, {}, tmp_path)
    names = ["test_core.py", "new-suite/test_native.py", "new-suite/nested/test_owned.py"]
    for name in names:
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_fixture(): pass\n")
    values = json.loads(workflow_steps.outputs(step, {}, tmp_path)[key])
    assert Counter(values) == Counter(names)
    for index in range(256):
        (base / f"test_matrix_{index}.py").write_text("def test_fixture(): pass\n")
    with pytest.raises(AssertionError):
        workflow_steps.outputs(step, {}, tmp_path)


@pytest.mark.parametrize("filename,job,root", [
    ("tests.yml", "e2e", "tests/e2e"),
    ("tests-os.yml", "e2e-windows", "tests/e2e/core/windows"),
])
def test_native_slices_keep_every_file_once(filename, job, root):
    data = workflow(filename)["jobs"][job]
    slices = data["strategy"]["matrix"]["slice"]
    assert slices == list(range(1, len(slices) + 1))
    files = sorted((ROOT / root).rglob("test_*.py"))
    if job == "e2e":
        files = [p for p in files if not p.is_relative_to(ROOT / "tests/e2e/core/upgrade")]
    buckets = [runner._slice_files(files, i, len(slices), {}, ROOT) for i in slices]
    assert all(buckets)
    assert Counter(p for bucket in buckets for p in bucket) == Counter(files)
    step = next(s for s in data["steps"] if "HERMES_TEST_SLICE" in s.get("env", {}))
    assert step["env"]["HERMES_TEST_WORKERS"] == "1"
    assert step["env"]["HERMES_TEST_FILE_RETRIES"] == "0"
    assert step["env"]["HERMES_E2E_STRICT_ACCEPTANCE"] == "${{ inputs.strict_acceptance }}"
    assert data["strategy"]["fail-fast"] is False
    assert data["strategy"]["max-parallel"] <= 4


def test_windows_crash_manifest_still_required_in_its_complete_journey():
    job = workflow("windows-install-update-e2e.yml")["jobs"]["install-update"]
    step = next(s for s in job["steps"] if s.get("name") == "Every crash cell ran")
    assert step["if"] == "matrix.file == 'test_crash_cells.py'"
    workflow_steps.required(step, {"matrix": {"file": "test_crash_cells.py"}})
    assert not step.get("continue-on-error", False)
    env = next(s["env"] for s in job["steps"] if s.get("name") == "Run Windows install + update E2E")
    assert env["HERMES_TEST_WORKERS"] == "1"
    assert env["HERMES_TEST_FILE_RETRIES"] == "0"
    assert env["HERMES_E2E_WINDOWS_INSTALL"] == "1"


def test_desktop_complete_native_shards_and_update_specs():
    core = workflow("e2e-desktop-core.yml")["jobs"]["core"]
    assert core["strategy"]["matrix"]["slice"] == list(range(1, 7))
    step = next(s for s in core["steps"] if "playwright test" in s.get("run", ""))
    command = step["run"].split("playwright test", 1)[1].split()
    assert command == ["-c", "e2e/core/playwright.config.ts", "--shard=${{", "matrix.slice", "}}/6", "--workers=1"]
    update = workflow("e2e-desktop-update.yml")["jobs"]["update"]
    expected = {p.relative_to(ROOT / "apps/desktop").as_posix()
                for p in (ROOT / "apps/desktop/e2e/update").glob("*.spec.ts")}
    selected = [p for cell in update["strategy"]["matrix"]["include"] for p in cell["specs"].split()]
    assert Counter(selected) == Counter(expected)
    assert core["strategy"]["max-parallel"] == update["strategy"]["max-parallel"] == 2
