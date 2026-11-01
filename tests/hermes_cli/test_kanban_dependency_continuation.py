"""Exercise the exact Hermes owner candidate against temporary native SQLite only.

Run with the owner checkout on PYTHONPATH and its Python interpreter. No live
board, guard, dispatcher, profile, or model is used.
"""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import kanban_db as kb


class DependencyContinuation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home / "home"),
            "HERMES_KANBAN_DB": str(self.home / "board.db"),
            "HERMES_KANBAN_HOME": str(self.home),
        })
        self.env.start()
        self.path_home = patch.object(Path, "home", return_value=self.home)
        self.path_home.start()
        kb.init_db()
        self.conn = kb.connect()

    def tearDown(self):
        self.conn.close()
        self.path_home.stop()
        self.env.stop()
        self.tmp.cleanup()

    def waiting(self):
        parent = kb.create_task(self.conn, title="fixture release", assignee="owner")
        tid = kb.create_task(self.conn, title="fixture existing followthrough", assignee="builder")
        run = kb.claim_task(self.conn, tid)
        self.assertIsNotNone(run)
        kb.add_comment(self.conn, tid, author="builder", body="Existing https://github.com/example/repo/pull/2370; retain history")
        kb.link_tasks(self.conn, parent, tid, expected_child_run_id=run.current_run_id)
        self.assertTrue(kb.block_task(self.conn, tid, reason="fixture release dependency", kind="dependency", expected_run_id=run.current_run_id))
        return tid, parent, run.current_run_id

    def resolved(self):
        tid, parent, run_id = self.waiting()
        prun = kb.claim_task(self.conn, parent)
        self.assertIsNotNone(prun)
        self.assertTrue(kb.complete_task(self.conn, parent, summary="fixture-only release", expected_run_id=prun.current_run_id))
        kb.recompute_ready(self.conn)
        self.assertEqual(kb.get_task(self.conn, tid).status, "ready")
        return tid, parent, run_id

    def test_resolved_dependency_continues_same_task_once_preserving_history(self):
        tid, _, old_run = self.resolved()
        comments = [(c.id, c.body) for c in kb.list_comments(self.conn, tid)]
        events = self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        # Parent completion permits the original owner to continue the existing PR.
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertEqual(events, self.conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0])
        claimed = kb.claim_task(self.conn, tid)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, tid)
        self.assertEqual(claimed.assignee, "builder")
        self.assertNotEqual(claimed.current_run_id, old_run)
        self.assertIsNone(kb.claim_task(self.conn, tid))
        self.assertEqual(comments, [(c.id, c.body) for c in kb.list_comments(self.conn, tid)])
        self.assertEqual(self.conn.execute("SELECT outcome FROM task_runs WHERE id=?", (old_run,)).fetchone()[0], "blocked")

    def test_existing_pr_remains_context(self):
        tid = kb.create_task(self.conn, title="fixture duplicate", assignee="builder")
        kb.add_comment(self.conn, tid, author="builder", body="https://github.com/example/repo/pull/2370")
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))

    def test_unresolved_dependency_cannot_be_claimed(self):
        tid, _, _ = self.waiting()
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_reopened_parent_revokes_continuation(self):
        tid, parent, _ = self.resolved()
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        self.conn.commit()
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_active_claim_never_gets_dependency_exception(self):
        tid, _, _ = self.resolved()
        self.assertIsNotNone(kb.claim_task(self.conn, tid))
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_dependency_metadata_does_not_create_a_pr_stop(self):
        mutations = {
            "wrong_run": "UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='dependency_wait'",
            "wrong_owner": "UPDATE tasks SET assignee='other' WHERE id=?",
            "missing_promote": "DELETE FROM task_events WHERE task_id=? AND kind='promoted'",
            "promote_before_wait": "UPDATE task_events SET kind=CASE kind WHEN 'promoted' THEN 'dependency_wait' ELSE 'promoted' END WHERE task_id=? AND kind IN ('promoted','dependency_wait')",
            "bad_payload": "UPDATE task_events SET payload='invalid' WHERE task_id=? AND kind='dependency_wait'",
            "wrong_kind": "UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='dependency_wait'",
            "no_parent": "DELETE FROM task_links WHERE child_id=?",
            "active_run": "UPDATE task_runs SET ended_at=NULL WHERE task_id=?",
            "new_comment": "INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,'owner','new source after promotion',1)",
            "active_lock": "UPDATE tasks SET claim_lock='fixture' WHERE id=?",
            "active_pid": "UPDATE tasks SET worker_pid=12345 WHERE id=?",
            "active_pointer": "UPDATE tasks SET current_run_id=12345 WHERE id=?",
        }
        for mutation, sql in mutations.items():
            with self.subTest(mutation=mutation):
                tid, _, _ = self.resolved()
                self.conn.execute(sql, (tid,))
                self.conn.commit()
                self.assertIsNone(kb.check_respawn_guard(self.conn, tid))

    def test_newer_run_consumes_old_continuation(self):
        tid, _, _ = self.resolved()
        claimed = kb.claim_task(self.conn, tid)
        self.assertTrue(kb.block_task(self.conn, tid, reason="fixture other blocker", kind="transient", expected_run_id=claimed.current_run_id))
        self.assertTrue(kb.unblock_task(self.conn, tid))
        kb.add_comment(self.conn, tid, author="owner", body="fixture later receipt")
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid))

    def test_auth_and_cooldown_still_precede_continuation(self):
        tid, _, run = self.resolved()
        self.conn.execute("UPDATE tasks SET last_failure_error='authentication failed' WHERE id=?", (tid,))
        self.conn.commit()
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "blocker_auth")
        self.conn.execute("UPDATE task_runs SET outcome='rate_limited',ended_at=? WHERE id=?", (int(kb.time.time()), run))
        self.conn.commit()
        with patch.object(kb, "_resolve_rate_limit_cooldown_seconds", return_value=300):
            self.assertEqual(kb.check_respawn_guard(self.conn, tid), "rate_limit_cooldown")

    def test_recent_success_still_precedes_continuation(self):
        tid, _, run = self.resolved()
        self.conn.execute("UPDATE task_runs SET outcome='completed',ended_at=? WHERE id=?", (int(kb.time.time()) + 1, run))
        self.conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (int(kb.time.time()) - 5, tid))
        self.conn.commit()
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "recent_success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
