"""Cross-repo contract, enabled against an explicitly provided bridge worktree."""
import importlib.util
import os
from pathlib import Path
import unittest
import pytest


def test_existing_oos_delivery_contract_with_native_limits():
    source = os.environ.get("MRPANACHE_TEST_BRIDGE_SOURCE")
    if not source:
        fixture = Path(__file__).resolve().parents[2] / ".pytest_cache/native-bridge-source"
        source = str(fixture) if (fixture / "tests/unit/mcp-server/hermes-oos-decision.test.py").is_file() else None
    if not source:
        pytest.skip("Set MRPANACHE_TEST_BRIDGE_SOURCE to the bridge source worktree")
    os.environ["MRPANACHE_TEST_NATIVE_RUNTIME"] = str(Path(__file__).resolve().parents[2])
    path = Path(source) / "tests/unit/mcp-server/hermes-oos-decision.test.py"
    spec = importlib.util.spec_from_file_location("native_run_limits_bridge_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    expected = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful()
    assert result.testsRun == expected and expected > 0
