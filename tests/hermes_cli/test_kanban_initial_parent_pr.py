"""Parent delivery references must not look like a child's already-open PR.

Only isolated native boards are mutated. No dispatcher or model is started.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import kanban_db as kb


class InitialParentPR(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "HERMES_HOME": self.tmp.name,
            "HERMES_KANBAN_HOME": self.tmp.name,
            "HERMES_KANBAN_DB": str(Path(self.tmp.name) / "board.db"),
        })
        self.env.start()
        self.conn = kb.connect()
        self.url = "https://github.com/example/parent-repo/pull/2470"
        self.parent = kb.create_task(self.conn, title="Parent delivery", assignee="parent-owner")
        run = kb.claim_task(self.conn, self.parent)
        assert run is not None
        self.parent_run = run.current_run_id
        self.child = kb.create_task(self.conn, title="Original child", assignee="child-owner",
                                    created_by="parent-owner", parents=[self.parent])
        kb.add_comment(self.conn, self.child, author="parent-owner", body="Parent draft " + self.url)
        kb.complete_task(self.conn, self.parent, summary="Parent draft delivered",
                         metadata={"pr_url": self.url}, expected_run_id=self.parent_run)
        kb.recompute_ready(self.conn)

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def test_initial_child_admitted_once_without_rewriting_history(self):
        before = list(self.conn.execute("SELECT * FROM task_events WHERE task_id=?", (self.child,)))
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))
        self.assertEqual(before, list(self.conn.execute("SELECT * FROM task_events WHERE task_id=?", (self.child,))))
        claimed = kb.claim_task(self.conn, self.child)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, self.child)
        self.assertEqual(claimed.assignee, "child-owner")
        self.assertIsNone(kb.claim_task(self.conn, self.child))
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))

    def test_unbound_pr_remains_context(self):
        kb.add_comment(self.conn, self.child, author="parent-owner",
                       body="Other https://github.com/example/parent-repo/pull/2471")
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))

    def test_owner_pr_remains_context_even_when_same_url(self):
        kb.add_comment(self.conn, self.child, author="child-owner", body=self.url)
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))

    def test_mixed_bound_and_unbound_urls_remain_context(self):
        kb.add_comment(self.conn, self.child, author="parent-owner",
                       body=self.url + " https://github.com/example/repo/pull/8")
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))

    def test_parent_pr_metadata_is_not_claim_authority(self):
        mutations = {
            "owner": ("UPDATE tasks SET assignee='other' WHERE id=?", (self.child,)),
            "creator": ("UPDATE tasks SET created_by='other' WHERE id=?", (self.child,)),
            "parent_owner": ("UPDATE tasks SET assignee='other' WHERE id=?", (self.parent,)),
            "parent_status": ("UPDATE tasks SET status='ready' WHERE id=?", (self.parent,)),
            "parent_run": ("UPDATE task_runs SET profile='other' WHERE id=?", (self.parent_run,)),
            "parent_outcome": ("UPDATE task_runs SET outcome='blocked' WHERE id=?", (self.parent_run,)),
            "metadata": ("UPDATE task_runs SET metadata='[]' WHERE id=?", (self.parent_run,)),
            "url": ("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"pr_url": self.url + "1"}), self.parent_run)),
            "no_parent": ("DELETE FROM task_links WHERE child_id=?", (self.child,)),
            "prior_run": ("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome) VALUES (?,?,'done',1,2,'blocked')", (self.child, "child-owner")),
            "pointer": ("UPDATE tasks SET current_run_id=99 WHERE id=?", (self.child,)),
            "lock": ("UPDATE tasks SET claim_lock='fixture' WHERE id=?", (self.child,)),
            "pid": ("UPDATE tasks SET worker_pid=999999 WHERE id=?", (self.child,)),
            "started": ("UPDATE tasks SET started_at=1 WHERE id=?", (self.child,)),
            "late_comment": ("UPDATE task_comments SET created_at=created_at+100 WHERE task_id=?", (self.child,)),
        }
        for mutation, (sql, args) in mutations.items():
            with self.subTest(mutation=mutation):
                self.conn.execute("SAVEPOINT metadata")
                self.conn.execute(sql, args)
                self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))
                self.conn.execute("ROLLBACK TO metadata")
                self.conn.execute("RELEASE metadata")

    def test_auth_guard_remains(self):
        self.conn.execute("UPDATE tasks SET last_failure_error='authentication failed' WHERE id=?", (self.child,))
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "blocker_auth")

    def test_newer_parent_run_does_not_make_pr_an_execution_stop(self):
        # A historical PR receipt is not a second execution policy.
        self.conn.execute("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome) "
                          "VALUES (?,?,'done',1,2,'blocked')", (self.parent, "parent-owner"))
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.child))


if __name__ == "__main__":
    unittest.main()
