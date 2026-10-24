"""Actual candidate transactions against existing serialized synthetic store.

No real Firebase/provider/account. Existing source/owner validators run with the
same explicit external-adapter doubles as the retained integrated harness.
"""

import importlib.util
import sys
from pathlib import Path
import unittest
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

W = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(W / 'ella_queue_resilience_v4/tests'))
spec = importlib.util.spec_from_file_location(
    'single_claim_harness', W / 'ella_queue_resilience_v4/tests/test_integrated_queue.py'
)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


class SingleClaimTests(h.IntegratedQueueTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        p = W / 'ella_single_claim_v1/candidate/database/normal_enrichment_activation.py'
        exec(compile(p.read_bytes(), str(p), 'exec'), cls.activation.__dict__)

    def setup_jobs(self):
        self.owner('one')
        self.owner('two')
        a = self.job('one', 'chosen')
        b = self.job('one', 'other')
        c = self.job('two', 'other')
        p = self.db.rows[self.activation.reference(self.db).key]
        p['queue_isolation']['claims_open'] = False
        self.db.put(self.activation.reference(self.db).key, p)
        return a, b, c

    def issue(self, job, **changes):
        p = self.activation.reference(self.db).get().to_dict()
        args = dict(
            job_id=job['job_id'],
            expected_policy_sha256=h.digest(p),
            expected_job_sha256=h.digest(self.db.rows[(self.activation.JOBS, job['job_id'])]),
            permit_id=str(uuid4()),
            now=h.NOW,
        )
        args.update(changes)
        return self.activation.issue_single_claim(self.db.transaction(), self.db, **args)

    def test_single_exact_job_reserved_once_then_begin_closes_atomically(self):
        a, b, c = self.setup_jobs()
        self.issue(a)
        self.assertIsNone(self.claim(b))
        self.assertIsNone(self.claim(c))
        claimed = self.claim(a)
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.claim(b))
        self.assertIsNone(self.claim(c))
        self.assertIsNone(self.claim(a))
        self.assertTrue(self.begin(claimed))
        p = self.activation.reference(self.db).get().to_dict()
        self.assertFalse(p['queue_isolation']['claims_open'])
        self.assertEqual(p[self.activation.SINGLE_CLAIM]['phase'], 'started')
        self.assertIsNone(self.claim(b))
        self.assertIsNone(self.claim(c))
        self.assertFalse(self.begin(claimed))

    def test_single_permit_rejects_epoch_owner_transcript_and_account_route_drift(self):
        for field in ['uid', 'transcript_sha256', 'admission_epoch', 'route_sha256', 'account_epoch']:
            with self.subTest(field=field):
                self.setUp()
                a, b, c = self.setup_jobs()
                self.issue(a)
                key = (self.activation.JOBS, a['job_id'])
                row = deepcopy(self.db.rows[key])
                if field in ['route_sha256', 'account_epoch']:
                    row[self.lifecycle.TAG][field] = 'changed'
                else:
                    row[field] = 'changed'
                self.db.put(key, row)
                self.assertIsNone(self.claim(row))

    def test_issue_cas_rejects_policy_or_job_changes(self):
        a, b, c = self.setup_jobs()
        for key in ['expected_policy_sha256', 'expected_job_sha256']:
            with self.assertRaises(ValueError):
                self.issue(a, **{key: '0' * 64})
        self.assertFalse(self.activation.reference(self.db).get().to_dict()['queue_isolation']['claims_open'])

    def test_expired_or_malformed_permit_fails_closed(self):
        for mutation in ['expiry', 'extra', 'phase', 'binding', 'lease']:
            with self.subTest(mutation=mutation):
                self.setUp()
                a, b, c = self.setup_jobs()
                self.issue(a)
                p = self.activation.reference(self.db).get().to_dict()
                v = p[self.activation.SINGLE_CLAIM]
                if mutation == 'expiry':
                    v['expires_at'] = h.NOW
                elif mutation == 'extra':
                    v['unexpected'] = True
                elif mutation == 'phase':
                    v['phase'] = 'started'
                elif mutation == 'binding':
                    v['binding']['uid'] = 'two'
                else:
                    v['lease_sha256'] = 'a' * 64
                self.db.put(self.activation.reference(self.db).key, p)
                self.assertIsNone(self.claim(a))

    def test_reserved_permit_wrong_lease_and_expiry_do_not_begin(self):
        a, b, c = self.setup_jobs()
        self.issue(a)
        q = self.claim(a)
        self.assertFalse(
            self.activation.begin(
                self.db.transaction(),
                self.db,
                self.db.collection(self.activation.JOBS).document(a['job_id']),
                lease_token='wrong',
                now=h.NOW,
            )
        )
        self.assertFalse(
            self.activation.begin(
                self.db.transaction(),
                self.db,
                self.db.collection(self.activation.JOBS).document(a['job_id']),
                lease_token=q['lease_token'],
                now=h.NOW + timedelta(minutes=11),
            )
        )

    def test_single_normal_source_owner_guard_still_required(self):
        a, b, c = self.setup_jobs()
        self.issue(a)
        account = self.account('one')
        account['state'] = 'paused'
        self.db.put(self.lifecycle.account_ref(self.db, 'one').key, account)
        self.assertIsNone(self.claim(a))

    def test_issue_unknown_commit_cannot_be_reissued(self):
        a, b, c = self.setup_jobs()
        self.db.commit_unknown = True
        with self.assertRaises(TimeoutError):
            self.issue(a)
        with self.assertRaises(ValueError):
            self.issue(a)
        self.assertIsNone(self.claim(b))
        self.assertIsNotNone(self.claim(a))

    def test_two_competing_claims_serialize_to_one(self):
        a, b, c = self.setup_jobs()
        self.issue(a)
        f = self.activation.claim.__wrapped__
        t1 = self.db.transaction()
        t2 = self.db.transaction()
        ref = self.db.collection(self.activation.JOBS).document(a['job_id'])
        first = f(t1, self.db, ref, now=h.NOW, lease_seconds=900)
        second = f(t2, self.db, ref, now=h.NOW, lease_seconds=900)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        t1.commit()
        with self.assertRaises(h.Conflict):
            t2.commit()
        self.assertIsNone(self.claim(b))
        self.assertIsNone(self.claim(c))

    def test_single_started_graph_can_pass_fence_while_claims_closed(self):
        a, b, c = self.setup_jobs()
        self.issue(a)
        q = self.claim(a)
        self.assertTrue(self.begin(q))
        # Full graph check verifies job, account, owner, route, source and lease;
        # maintenance closes only new claims, not this already-started attempt.
        self.graph.require_in_transaction(self.db.transaction(), self.db, q, q['uid'], q['conversation_id'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
