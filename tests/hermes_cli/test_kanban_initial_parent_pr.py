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
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")

    def test_unbound_pr_stays_guarded(self):
        kb.add_comment(self.conn, self.child, author="parent-owner",
                       body="Other https://github.com/example/parent-repo/pull/2471")
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")

    def test_owner_pr_stays_guarded_even_when_same_url(self):
        kb.add_comment(self.conn, self.child, author="child-owner", body=self.url)
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")

    def test_mixed_bound_and_unbound_urls_stay_guarded(self):
        kb.add_comment(self.conn, self.child, author="parent-owner",
                       body=self.url + " https://github.com/example/repo/pull/8")
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")

    def test_changed_or_missing_native_binding_denies(self):
        for mutation in ("owner", "creator", "parent_owner", "parent_status", "parent_run",
                         "parent_outcome", "metadata", "url", "no_parent", "prior_run", "pointer", "lock", "pid", "started", "late_comment"):
            with self.subTest(mutation=mutation):
                self.conn.execute("SAVEPOINT negative")
                if mutation == "owner":
                    self.conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (self.child,))
                elif mutation == "creator":
                    self.conn.execute("UPDATE tasks SET created_by='other' WHERE id=?", (self.child,))
                elif mutation == "parent_owner":
                    self.conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (self.parent,))
                elif mutation == "parent_status":
                    self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.parent,))
                elif mutation == "parent_run":
                    self.conn.execute("UPDATE task_runs SET profile='other' WHERE id=?", (self.parent_run,))
                elif mutation == "parent_outcome":
                    self.conn.execute("UPDATE task_runs SET outcome='blocked' WHERE id=?", (self.parent_run,))
                elif mutation == "metadata":
                    self.conn.execute("UPDATE task_runs SET metadata='[]' WHERE id=?", (self.parent_run,))
                elif mutation == "url":
                    self.conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (json.dumps({"pr_url": self.url + "1"}), self.parent_run))
                elif mutation == "no_parent":
                    self.conn.execute("DELETE FROM task_links WHERE child_id=?", (self.child,))
                elif mutation == "prior_run":
                    self.conn.execute("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome) VALUES (?,?, 'done',1,2,'blocked')", (self.child, "child-owner"))
                elif mutation == "late_comment":
                    self.conn.execute("UPDATE task_comments SET created_at=created_at+100 WHERE task_id=?", (self.child,))
                else:
                    column, value = {"pointer": ("current_run_id", 99), "lock": ("claim_lock", "fixture"),
                                     "pid": ("worker_pid", 999999), "started": ("started_at", 1)}[mutation]
                    self.conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, self.child))
                self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")
                self.conn.execute("ROLLBACK TO negative")
                self.conn.execute("RELEASE negative")

    def test_auth_guard_remains(self):
        self.conn.execute("UPDATE tasks SET last_failure_error='authentication failed' WHERE id=?", (self.child,))
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "blocker_auth")

    def test_newer_parent_run_revokes_old_delivery(self):
        # A later run must not authorize a child using a stale completed run.
        self.conn.execute("INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome) "
                          "VALUES (?,?,'done',1,2,'blocked')", (self.parent, "parent-owner"))
        self.assertEqual(kb.check_respawn_guard(self.conn, self.child), "active_pr")


if __name__ == "__main__":
    unittest.main()
