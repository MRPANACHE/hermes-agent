"""Exercise the exact Hermes owner candidate against temporary native SQLite only.

Run with the owner checkout on PYTHONPATH and its Python interpreter. No live
board, guard, dispatcher, profile, or model is used.
"""
import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_graph as graph
from hermes_cli.kanban import kanban_command
from hermes_cli.kanban_parser import build_parser


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

    def task_status(self, task_id):
        task = kb.get_task(self.conn, task_id)
        if task is None:
            raise AssertionError(f"missing fixture task: {task_id}")
        return task.status

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
        # Dependency satisfaction alone cannot vouch for this independent PR.
        self.assertEqual(kb.check_respawn_guard(self.conn, tid), "active_pr")
        states = {"https://github.com/example/repo/pull/2370": "MERGED"}
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid, pr_states=states))
        self.assertIsNone(kb.check_respawn_guard(self.conn, tid, pr_states=states))
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

    def test_archiving_parent_skips_replaced_dependency_and_preserves_successor_hold(self):
        parent = kb.create_task(self.conn, title="fixture superseded parent", assignee="owner")
        obsolete = kb.create_task(
            self.conn, title="fixture replaced dependent", assignee="builder", parents=(parent,),
        )
        kb.add_comment(self.conn, obsolete, author="owner", body="Superseded by the replacement task.")
        history_before = [(event.id, event.kind) for event in kb.list_events(self.conn, obsolete)]
        comments_before = [(comment.id, comment.body) for comment in kb.list_comments(self.conn, obsolete)]
        dependencies_before = kb.unsatisfied_parents(self.conn, obsolete)

        hold = kb.create_task(self.conn, title="fixture operator hold", assignee="owner")
        self.assertTrue(kb.block_task(self.conn, hold, reason="fixture hold", kind="needs_input"))
        successor = kb.create_task(
            self.conn, title="fixture successor", assignee="builder", parents=(hold,),
        )
        valid_child = kb.create_task(
            self.conn, title="fixture valid dependent", assignee="builder", parents=(parent,),
        )
        self.assertEqual(self.task_status(obsolete), "todo")
        self.assertEqual(self.task_status(successor), "todo")
        self.assertEqual(self.task_status(valid_child), "todo")

        parser = argparse.ArgumentParser()
        build_parser(parser.add_subparsers())
        args = parser.parse_args(["kanban", "supersede", obsolete, successor])
        self.assertEqual(kanban_command(args), 0)
        self.assertEqual(self.task_status(obsolete), "todo")
        self.assertEqual(kb.unsatisfied_parents(self.conn, obsolete), dependencies_before)

        events = kb.list_events(self.conn, obsolete)
        self.assertEqual(
            [(event.id, event.kind) for event in events[:len(history_before)]],
            history_before,
        )
        self.assertEqual(events[-1].kind, "superseded")
        self.assertEqual(events[-1].payload, {"successor_id": successor})
        self.assertEqual(
            comments_before,
            [(comment.id, comment.body) for comment in kb.list_comments(self.conn, obsolete)],
        )

        # Ordinary archived-parent semantics remain intact for non-replaced work.
        self.assertTrue(kb.archive_task(self.conn, parent))
        self.assertEqual(self.task_status(valid_child), "ready")
        self.assertIn("promoted", [event.kind for event in kb.list_events(self.conn, valid_child)])
        self.assertEqual(self.task_status(obsolete), "todo")
        self.assertNotIn("promoted", [event.kind for event in kb.list_events(self.conn, obsolete)])

        # The replacement's independent blocked parent remains a hold.
        self.assertEqual(self.task_status(hold), "blocked")
        self.assertEqual(self.task_status(successor), "todo")
        self.assertEqual(kb.unsatisfied_parents(self.conn, successor), [(hold, "blocked")])
        self.assertNotIn("promoted", [event.kind for event in kb.list_events(self.conn, successor)])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id IN (?, ?)", (obsolete, successor),
        ).fetchone()[0], 0)

    def test_superseded_event_stops_later_promotion_and_claim(self):
        obsolete = kb.create_task(self.conn, title="fixture obsolete", assignee="builder")
        successor = kb.create_task(self.conn, title="fixture successor", assignee="builder")
        self.assertTrue(graph.supersede_task(self.conn, obsolete, successor))
        self.assertEqual(self.task_status(obsolete), "ready")

        # Model a stale status restore: durable supersession still gates every native start path.
        self.conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (obsolete,))
        self.conn.commit()
        self.assertEqual(kb.recompute_ready(self.conn), 0)
        self.assertEqual(self.task_status(obsolete), "todo")
        promoted, reason = kb.promote_task(self.conn, obsolete, actor="fixture")
        self.assertFalse(promoted)
        self.assertIn("superseded", reason)

        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (obsolete,))
        self.conn.commit()
        self.assertIsNone(kb.claim_task(self.conn, obsolete))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (obsolete,),
        ).fetchone()[0], 0)
        self.assertNotIn("claimed", [event.kind for event in kb.list_events(self.conn, obsolete)])

    def test_supersession_requires_a_distinct_existing_successor_atomically(self):
        obsolete = kb.create_task(self.conn, title="fixture invalid supersession", assignee="builder")
        before = [(event.id, event.kind) for event in kb.list_events(self.conn, obsolete)]
        for successor in (obsolete, "missing-successor"):
            with self.subTest(successor=successor):
                with self.assertRaises(ValueError):
                    graph.supersede_task(self.conn, obsolete, successor)
                self.assertEqual(self.task_status(obsolete), "ready")
                self.assertEqual(
                    [(event.id, event.kind) for event in kb.list_events(self.conn, obsolete)],
                    before,
                )

        successor = kb.create_task(self.conn, title="fixture replacement", assignee="builder")
        self.assertTrue(graph.supersede_task(self.conn, obsolete, successor))
        self.assertTrue(graph.supersede_task(self.conn, obsolete, successor))
        self.assertEqual(
            sum(event.kind == "superseded" for event in kb.list_events(self.conn, obsolete)),
            1,
        )
        successor_events = [(event.id, event.kind) for event in kb.list_events(self.conn, successor)]
        with self.assertRaisesRegex(ValueError, "supersession would create a cycle"):
            graph.supersede_task(self.conn, successor, obsolete)
        self.assertEqual(
            [(event.id, event.kind) for event in kb.list_events(self.conn, successor)],
            successor_events,
        )

    def test_running_task_must_be_stopped_before_supersession(self):
        obsolete = kb.create_task(self.conn, title="fixture active obsolete", assignee="builder")
        successor = kb.create_task(self.conn, title="fixture replacement", assignee="builder")
        run = kb.claim_task(self.conn, obsolete)
        self.assertIsNotNone(run)

        with self.assertRaisesRegex(ValueError, "stop its worker through the native lifecycle first"):
            graph.supersede_task(self.conn, obsolete, successor)

        self.assertEqual(self.task_status(obsolete), "running")
        self.assertNotIn("superseded", [event.kind for event in kb.list_events(self.conn, obsolete)])

    def test_supersession_survives_event_retention(self):
        obsolete = kb.create_task(self.conn, title="obsolete", assignee="builder")
        successor = kb.create_task(self.conn, title="replacement", assignee="builder")
        graph.supersede_task(self.conn, obsolete, successor)
        kb.archive_task(self.conn, obsolete)
        self.conn.execute("UPDATE task_events SET created_at=1 WHERE task_id=?", (obsolete,))
        self.conn.commit()
        kb.gc_events(self.conn, older_than_seconds=0)
        self.assertEqual([e.kind for e in kb.list_events(self.conn, obsolete)], ["superseded"])
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (obsolete,))
        self.conn.commit()
        self.assertIsNone(kb.claim_task(self.conn, obsolete))


if __name__ == "__main__":
    unittest.main(verbosity=2)
