"""Typed same-task continuation: isolated native board, no production workers."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_cli import kanban_db as kb


class ReadyAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name,
            'HERMES_KANBAN_HOME': self.tmp.name,
            'HERMES_KANBAN_DB': str(Path(self.tmp.name) / 'board.db')})
        self.env.start()
        self.conn = kb.connect()
        self.tid = kb.create_task(self.conn, title='Existing continuation', assignee='owner')
        claimed = kb.claim_task(self.conn, self.tid)
        self.run = claimed.current_run_id
        with kb.write_txn(self.conn):
            kb._append_event(self.conn, self.tid, 'spawned', {'pid': 999999991}, run_id=self.run)
        kb.add_comment(self.conn, self.tid, 'owner', 'https://github.com/example/repo/pull/11')
        kb.block_task(self.conn, self.tid, kind='needs_input', reason='Original question', expected_run_id=self.run)
        self.block = kb.list_events(self.conn, self.tid)[-1].id
        with kb.write_txn(self.conn):
            comment = kb.add_comment(self.conn, self.tid, 'mrpanache-mcp', 'Original explicit resume')
            kb.unblock_task(self.conn, self.tid, allow_nested=True)
            kb._append_event(self.conn, self.tid, 'mrpanache_resumed', {
                'action': 'resume', 'request_id': 'original', 'fingerprint': 'a' * 64,
                'previous_run_id': self.run, 'block_event_id': self.block, 'comment_id': comment})
        kb.add_comment(self.conn, self.tid, 'owner', 'Later readback invalidates old wake')
        self.assertEqual(kb.check_respawn_guard(self.conn, self.tid), 'active_pr')

    def tearDown(self):
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def admit(self):
        with kb.write_txn(self.conn):
            binding = kb.ready_resume_binding(self.conn, self.tid)
            body = 'New exact explicit continuation, unchanged mandate'
            comment = kb.add_comment(self.conn, self.tid, 'mrpanache-mcp', body)
            ce = kb.list_events(self.conn, self.tid)[-1].id
            kb._append_event(self.conn, self.tid, 'mrpanache_resumed', {
                'action': 'resume', 'request_id': 'fresh', 'fingerprint': 'b' * 64,
                'previous_run_id': self.run, 'block_event_id': self.block, 'comment_id': comment,
                'ready_admission': {'binding': binding, 'decision_comment_event_id': ce,
                                    'comment_sha256': hashlib.sha256(body.encode()).hexdigest()}})

    def test_explicit_admission_guards_normal_claim_once(self):
        self.admit()
        self.assertTrue(kb.ready_resume_admitted(self.conn, self.tid))
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.tid))
        self.assertIsNotNone(kb.claim_task(self.conn, self.tid))
        self.assertIsNone(kb.claim_task(self.conn, self.tid))

    def test_later_control_revokes_claim_between_dispatcher_read_and_cas(self):
        self.admit()
        self.assertIsNone(kb.check_respawn_guard(self.conn, self.tid))
        kb.add_comment(self.conn, self.tid, 'mrpanache-mcp', 'Stop; a new question')
        self.assertEqual(kb.check_respawn_guard(self.conn, self.tid), 'active_pr')
        self.assertIsNone(kb.claim_task(self.conn, self.tid))

    def test_binding_changes_and_unknown_worker_deny(self):
        self.admit()
        self.conn.execute('UPDATE tasks SET workspace_path=? WHERE id=?', ('/changed', self.tid))
        self.conn.commit()
        self.assertFalse(kb.ready_resume_admitted(self.conn, self.tid))
        self.assertIsNone(kb.claim_task(self.conn, self.tid))
        with patch('psutil.pid_exists', side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, 'worker_exit_not_confirmed'):
                kb.ready_resume_binding(self.conn, self.tid)

    def test_active_worker_and_review_failclosed(self):
        self.conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (os.getpid(), self.tid))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'worker_exit_not_confirmed'):
            kb.ready_resume_binding(self.conn, self.tid)
        self.conn.execute("UPDATE tasks SET worker_pid=NULL,status='review' WHERE id=?", (self.tid,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'ready_not_resumable'):
            kb.ready_resume_binding(self.conn, self.tid)

    def test_parent_reopened_remains_guard(self):
        parent = kb.create_task(self.conn, title='Parent', assignee='owner')
        run = kb.claim_task(self.conn, parent)
        kb.complete_task(self.conn, parent, expected_run_id=run.current_run_id)
        kb.link_tasks(self.conn, parent, self.tid)
        self.admit()
        self.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        self.conn.commit()
        self.assertFalse(kb.ready_resume_admitted(self.conn, self.tid))
        self.assertIsNone(kb.claim_task(self.conn, self.tid))

    def test_auth_still_precedes_admission(self):
        self.admit()
        self.conn.execute('UPDATE tasks SET last_failure_error=? WHERE id=?', ('authentication failed', self.tid))
        self.conn.commit()
        self.assertEqual(kb.check_respawn_guard(self.conn, self.tid), 'blocker_auth')
        self.assertIsNone(kb.claim_task(self.conn, self.tid))

    def test_stale_original_block_run_invalidates(self):
        self.conn.execute('UPDATE task_events SET run_id=0 WHERE id=?', (self.block,))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'ready_lineage_invalid'):
            kb.ready_resume_binding(self.conn, self.tid)

    def test_malformed_receipt_fails_closed(self):
        self.admit()
        row = self.conn.execute("SELECT id,payload FROM task_events WHERE task_id=? "
                                "AND kind='mrpanache_resumed' ORDER BY id DESC LIMIT 1", (self.tid,)).fetchone()
        for payload in ('[]', '{', json.dumps({'ready_admission': []})):
            self.conn.execute('UPDATE task_events SET payload=? WHERE id=?', (payload, row['id']))
            self.conn.commit()
            self.assertFalse(kb.ready_resume_admitted(self.conn, self.tid))


if __name__ == '__main__':
    unittest.main()
