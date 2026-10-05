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
        kb.link_tasks(self.conn, parent, tid)
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

    def test_plain_duplicate_pr_stays_guarded(self):
        tid = kb.create_task(self.conn, title="fixture duplicate", assignee="builder")
        kb.add_comment(self.conn, tid, author="builder", body="https://github.com/example/repo/pull/2370")
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")

    def test_unresolved_dependency_cannot_bypass_pr_or_claim(self):
        tid, _, _ = self.waiting()
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_reopened_parent_revokes_continuation(self):
        tid, parent, _ = self.resolved()
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        self.conn.commit()
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_active_claim_never_gets_dependency_exception(self):
        tid, _, _ = self.resolved()
        self.assertIsNotNone(kb.claim_task(self.conn, tid))
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")
        self.assertIsNone(kb.claim_task(self.conn, tid))

    def test_stale_or_invalid_dependency_sources_stay_guarded(self):
        for mutation in ("wrong_run", "wrong_owner", "missing_promote", "promote_before_wait", "bad_payload", "wrong_kind", "no_parent", "active_lock", "active_pid", "active_pointer", "active_run", "new_comment"):
            with self.subTest(mutation=mutation):
                tid, _, _ = self.resolved()
                if mutation == "wrong_run":
                    self.conn.execute("UPDATE task_events SET run_id=NULL WHERE task_id=? AND kind='dependency_wait'", (tid,))
                elif mutation == "wrong_owner":
                    self.conn.execute("UPDATE tasks SET assignee='other' WHERE id=?", (tid,))
                elif mutation == "missing_promote":
                    self.conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='promoted'", (tid,))
                elif mutation == "promote_before_wait":
                    self.conn.execute("UPDATE task_events SET kind=CASE kind WHEN 'promoted' THEN 'dependency_wait' ELSE 'promoted' END WHERE task_id=? AND kind IN ('promoted','dependency_wait')", (tid,))
                elif mutation == "bad_payload":
                    self.conn.execute("UPDATE task_events SET payload='invalid' WHERE task_id=? AND kind='dependency_wait'", (tid,))
                elif mutation == "wrong_kind":
                    self.conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='dependency_wait'", (tid,))
                elif mutation == "no_parent":
                    self.conn.execute("DELETE FROM task_links WHERE child_id=?", (tid,))
                elif mutation == "active_run":
                    self.conn.execute("UPDATE task_runs SET ended_at=NULL WHERE task_id=?", (tid,))
                elif mutation == "new_comment":
                    kb.add_comment(self.conn, tid, author="owner", body="new source after promotion")
                else:
                    column, value = {"active_lock": ("claim_lock", "fixture"), "active_pid": ("worker_pid", 12345), "active_pointer": ("current_run_id", 12345)}[mutation]
                    self.conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))
                self.conn.commit()
                self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")

    def test_newer_run_consumes_old_continuation(self):
        tid, _, _ = self.resolved()
        claimed = kb.claim_task(self.conn, tid)
        self.assertTrue(kb.block_task(self.conn, tid, reason="fixture other blocker", kind="transient", expected_run_id=claimed.current_run_id))
        self.assertTrue(kb.unblock_task(self.conn, tid))
        kb.add_comment(self.conn, tid, author="owner", body="fixture later receipt")
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")

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
