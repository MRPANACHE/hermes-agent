# Synthetic native recovery composition fixture

This is OFFLINE qualification, not production activation. No source job, profile
DB, OAuth storage, or PB endpoint is opened. HTTP is synthetic at the lowest
socket boundary; the real ClientSession, HTTP transport/auth guards, owner
lease, private socketpair, Node process, queue implementation and SQLite execute.

`consumer/src/` is the unchanged ten-module import closure from sealed PB revision
`3d2ce28882926b7cef2066bc0d04ce7768605496`. `source-manifest.json` binds every file
and the release-seal digest. The only NPM dependency is exactly locked
better-sqlite3 12.9.0, installed normally with the SAME `/usr/bin/node` that the
owner invokes. No runtime profiles or PYTHONPATH overlay are used.

Preparation (the NPM cache can be supplied offline with `--offline --cache ...`):

    PATH="/usr/bin:/bin:$PATH" npm ci --prefix tests/e2e/pax_recovery/consumer --no-audit --no-fund

Then use the supported runner, at most two workers, no file retries:

    HERMES_PYTHON=/path/to/isolated/venv/bin/python bash scripts/run_tests.sh -j 2 --file-retries 0 --include-integration tests/e2e/pax_recovery/test_owner_sdk_consumer.py

`fixture.mjs` replaces ONLY the production binding's release/job preflight with a
synthetic job/native-DB fixture. It calls the unchanged queue implementation
through the unchanged private-channel implementation. Consequently these tests
do not prove production binding preflight against a live source; exact-head
review, separate release/live disposition, and fresh source preflight remain
mandatory. Fault injection affects only synthetic temp jobs and audit files.

The companion SDK test includes auth/status denials and queued cancellation.
A denied HTTP writer can terminate its SDK transport taskgroup. That is a
terminal failure, NOT a working reusable connection; no rediscovery/reconnect
or auth fallback is allowed by this owner action. The tests check only the
specific expected exception leaves and ensure frozen guards/active reads clear.
