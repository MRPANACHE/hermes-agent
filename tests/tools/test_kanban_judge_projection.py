"""Deterministic judge projection contracts; no live board writes."""
import hashlib
import json
import sqlite3
import unittest
from types import SimpleNamespace

from tools.kanban_tools import _goal_handoff_context


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE task_events(id INTEGER, task_id TEXT, kind TEXT, payload TEXT)')
        self.task = SimpleNamespace(id='original', title='Review exact draft', goal_mode=True,
                                    assignee='noor', body=json.dumps({
                                        'schema': 'mrpanache.agent-request.v1', 'agent': 'noor',
                                        'kind': 'codework', 'delivery_goal': 'draft_pr'}) + '\nOriginal constraints unchanged.')
        self.comments = []
        self.runs = [SimpleNamespace(id=i, started_at=i * 100, status='blocked',
                                     outcome='blocked', summary='stored result' * 10000,
                                     error=None, metadata=None) for i in (201, 204, 210)]
        self.kb = SimpleNamespace(list_comments=lambda *_: self.comments, list_runs=lambda *_: self.runs)

    def tearDown(self):
        self.conn.close()

    def comment(self, ident, author, body, created_at=1):
        c = SimpleNamespace(id=ident, author=author, body=body, created_at=created_at)
        self.comments.append(c)
        return c

    def amendment(self, ident=1, **changes):
        self.comment(ident, 'mrpanache-mcp', 'Preserve draft-only; no merge/deploy.')
        payload = dict(comment_id=ident, request_id='request-exact', fingerprint='a' * 64, delivery_goal='draft_pr')
        payload.update(changes)
        self.conn.execute('INSERT INTO task_events VALUES(?,?,?,?)', (ident, 'original', 'mrpanache_resumed', json.dumps(payload)))

    def project(self):
        return _goal_handoff_context(self.kb, self.conn, self.task)

    def test_large_history_is_deterministic_and_lossless_by_reference(self):
        self.amendment()
        old = self.comment(2, 'noor', 'historical worker log' * 10000)
        # Latest run is active evidence and must fit; old large summaries only refs.
        self.runs[-1].summary = 'Current exact evidence'
        current = self.comment(3, 'noor', 'Current draft PR evidence', 21000)
        first = self.project()
        self.assertEqual(first, self.project())
        self.assertLess(len(first), 98000)
        data = json.loads(first.split('\n', 1)[1])
        self.assertEqual(data['original_body'], self.task.body)
        self.assertEqual(data['verified_requester_amendments'][0]['body'], self.comments[0].body)
        self.assertEqual(data['active_comments_untruncated'][0]['body'], current.body)
        self.assertEqual(data['historical_worker_comment_refs'][0]['sha256'], hashlib.sha256(old.body.encode()).hexdigest())
        self.assertEqual(old.body, 'historical worker log' * 10000)
        self.assertEqual(len(data['stored_run_refs']), 3)

    def test_current_constraints_over_budget_fail_closed(self):
        self.comment(1, 'reviewer', 'constraint' * 20000)
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.project()

    def test_original_body_over_budget_fail_closed(self):
        self.task.body += 'original constraint' * 10000
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.project()

    def test_changed_author_provenance_denied(self):
        self.amendment()
        self.comments[0].author = 'noor'
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.project()

    def test_missing_comment_ref_denied(self):
        self.amendment(comment_id=999)
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.project()

    def test_same_timestamp_and_shuffled_source_are_deterministic(self):
        self.runs[-1].summary = None
        self.comment(2, 'noor', 'second', 1)
        self.comment(1, 'noor', 'first', 1)
        first = self.project()
        self.comments.reverse()
        self.runs.reverse()
        self.assertEqual(first, self.project())

    def test_malformed_provenance_denied(self):
        for changes in ({'fingerprint': {'bad': 'object'}}, {'fingerprint': 'bad'},
                        {'request_id': ['not-string']}, {'comment_id': True}):
            with self.subTest(changes=changes):
                self.comments.clear()
                self.conn.execute('DELETE FROM task_events')
                self.amendment(**changes)
                with self.assertRaisesRegex(ValueError, 'provenance'):
                    self.project()

    def test_missing_fingerprint_denied(self):
        self.amendment(fingerprint=None)
        with self.assertRaisesRegex(ValueError, 'provenance'):
            self.project()

    def test_unbound_requester_comment_denied(self):
        self.comment(1, 'mrpanache-mcp', 'new constraint without native receipt')
        with self.assertRaisesRegex(ValueError, 'native event'):
            self.project()

    def test_unauthorized_delivery_change_denied(self):
        self.amendment(delivery_goal='verified_live')
        with self.assertRaisesRegex(ValueError, 'unsupported delivery'):
            self.project()

    def test_clarification_without_delivery_is_exact(self):
        self.amendment(delivery_goal=None)
        self.runs[-1].summary = None
        self.conn.execute("UPDATE task_events SET kind='mrpanache_clarified'")
        data = json.loads(self.project().split('\n', 1)[1])
        self.assertEqual(data['effective_delivery_goal'], 'draft_pr')
        self.assertEqual(data['verified_requester_amendments'][0]['body'], self.comments[0].body)

    def test_unknown_authors_are_never_archived_as_worker_logs(self):
        self.runs[-1].summary = None
        self.comment(1, 'reviewer', 'Mandatory active correction', 1)
        self.assertIn('Mandatory active correction', self.project())

    def test_legacy_without_ingress_stays_supported(self):
        self.runs[-1].summary = None
        self.task.body = 'Legacy exact task body'
        self.assertIn(self.task.body, self.project())


if __name__ == '__main__':
    unittest.main()
