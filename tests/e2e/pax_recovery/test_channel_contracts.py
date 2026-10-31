"""Put the delivered native channel contracts in normal E2E discovery."""
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).parent
INTEGRATION = HERE.parents[2] / "integrations" / "pax-recovery-native"


def _load(filename):
    spec = importlib.util.spec_from_file_location(f"pax_recovery_{filename}", INTEGRATION / f"{filename}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# These are the original delivered unittest cases, not reimplementations.
Composition = _load("test_composition").Composition
Wire = _load("test_wire").Wire


def test_sealed_consumer_fixture_integrity():
    root = HERE / "consumer"
    manifest = json.loads((root / "source-manifest.json").read_text())
    assert manifest["source_revision"] == "3d2ce28882926b7cef2066bc0d04ce7768605496"
    assert manifest["release_seal_sha256"] == "9fe72068081804dd871c472e0733020773125dced2e9cf1b416b7bbe8f582765"
    assert len(manifest["files"]) == 10
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert package["dependencies"] == {"better-sqlite3": "12.9.0"}
    assert lock["packages"]["node_modules/better-sqlite3"]["version"] == "12.9.0"
