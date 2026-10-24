"""Synthetic state-machine tests; no imports from serving code or network."""

import dataclasses
import importlib.util
from pathlib import Path
import sys
import unittest

PATH = Path(__file__).resolve().parents[1] / 'candidate/database/enrichment_attempt_fence.py'
SPEC = importlib.util.spec_from_file_location('attempt_fence_candidate', PATH)
fence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fence
SPEC.loader.exec_module(fence)


class AttemptFenceTests(unittest.TestCase):
    def setUp(self):
        self.attempt = fence.Attempt(
            'synthetic-owner', 'job-one', 'source-one', 'client-one', 'a' * 64, 'b' * 64, 1, 'lease-one'
        )
        self.reserved = fence.reserve(self.attempt, claims_open=True, previously_quarantined=False)

    def started(self):
        return fence.begin(self.reserved, self.attempt, claims_open=True)

    def effect(self):
        return fence.start_effect(
            self.started(), self.attempt, operation_id='op-one', kind='generator', request_sha256='c' * 64
        )

    def test_all_identity_fields_and_attempt_fence_are_enforced(self):
        for key, value in [
            ('uid', 'foreign'),
            ('job_id', 'other'),
            ('conversation_id', 'other'),
            ('client_interaction_id', 'other'),
            ('transcript_sha256', 'd' * 64),
            ('route_sha256', 'e' * 64),
            ('attempt', 2),
            ('lease_token', 'lease-two'),
        ]:
            with self.subTest(key=key), self.assertRaises(fence.FenceRejected):
                fence.begin(self.reserved, dataclasses.replace(self.attempt, **{key: value}), claims_open=True)

    def test_close_wins_race_late_effect_cannot_start(self):
        closed = fence.finish(self.started(), self.attempt, outcome='unknown_quarantined')
        with self.assertRaises(fence.FenceRejected):
            fence.start_effect(closed, self.attempt, operation_id='late', kind='generator', request_sha256='c' * 64)

    def test_start_wins_race_unknown_remains_resource_fenced(self):
        closed = fence.finish(self.effect(), self.attempt, outcome='unknown_quarantined')
        self.assertEqual(fence.unresolved_effects(closed, self.attempt), ('op-one',))
        self.assertFalse(fence.callback_allowed(closed, self.attempt))

    def test_late_receipt_releases_only_effect_never_reopens_job(self):
        closed = fence.finish(self.effect(), self.attempt, outcome='unknown_quarantined')
        settled = fence.complete_effect(
            closed, self.attempt, operation_id='op-one', request_sha256='c' * 64, receipt_sha256='d' * 64
        )
        self.assertEqual(fence.unresolved_effects(settled, self.attempt), ())
        self.assertEqual(settled['outcome'], 'unknown_quarantined')
        self.assertFalse(fence.callback_allowed(settled, self.attempt))
        with self.assertRaises(fence.FenceRejected):
            fence.retry_key(settled, self.attempt, next_lease_token='lease-two', max_attempts=3)

    def test_preflight_retry_is_monotonic_and_rejects_old_attempt(self):
        closed = fence.finish(
            self.reserved, self.attempt, outcome='verified_no_dispatch', preflight_code='account_runtime_not_ready'
        )
        successor = fence.retry_key(closed, self.attempt, next_lease_token='lease-two', max_attempts=3)
        new = fence.reserve(successor, claims_open=True, previously_quarantined=False)
        self.assertEqual(successor.attempt, 2)
        with self.assertRaises(fence.FenceRejected):
            fence.finish(new, self.attempt, outcome='unknown_quarantined')
        with self.assertRaises(fence.FenceRejected):
            fence.begin(new, self.attempt, claims_open=True)
        self.assertEqual(new['phase'], 'reserved')

    def test_known_code_does_not_make_started_attempt_retryable(self):
        for document in (self.started(), self.effect()):
            with self.assertRaises(fence.FenceRejected):
                fence.finish(
                    document, self.attempt, outcome='verified_no_dispatch', preflight_code='account_runtime_not_ready'
                )

    def test_duplicate_start_is_not_permission_to_repeat_network(self):
        with self.assertRaises(fence.FenceRejected):
            fence.start_effect(
                self.effect(), self.attempt, operation_id='op-one', kind='generator', request_sha256='c' * 64
            )

    def test_unknown_never_retries_without_effects_either(self):
        closed = fence.finish(self.reserved, self.attempt, outcome='unknown_quarantined')
        with self.assertRaises(fence.FenceRejected):
            fence.retry_key(closed, self.attempt, next_lease_token='lease-two', max_attempts=3)

    def test_maintenance_blocks_claim_and_begin_but_not_inflight_completion(self):
        with self.assertRaises(fence.FenceRejected):
            fence.reserve(self.attempt, claims_open=False, previously_quarantined=False)
        with self.assertRaises(fence.FenceRejected):
            fence.begin(self.reserved, self.attempt, claims_open=False)
        complete = fence.complete_effect(
            self.effect(), self.attempt, operation_id='op-one', request_sha256='c' * 64, receipt_sha256='d' * 64
        )
        terminal = fence.finish(complete, self.attempt, outcome='completed')
        self.assertEqual(terminal['outcome'], 'completed')

    def test_quarantine_cannot_be_resubmitted_as_new_reservation(self):
        with self.assertRaises(fence.FenceRejected):
            fence.reserve(self.attempt, claims_open=True, previously_quarantined=True)

    def test_completion_and_receipt_mismatch_fail_closed(self):
        with self.assertRaises(fence.FenceRejected):
            fence.finish(self.effect(), self.attempt, outcome='completed')
        with self.assertRaises(fence.FenceRejected):
            fence.complete_effect(
                self.effect(), self.attempt, operation_id='op-one', request_sha256='e' * 64, receipt_sha256='d' * 64
            )

    def test_retry_bounds_and_same_lease_are_rejected(self):
        closed = fence.finish(
            self.reserved, self.attempt, outcome='verified_no_dispatch', preflight_code='account_model_unavailable'
        )
        for maximum, lease in [(1, 'next'), (6, 'next'), (3, self.attempt.lease_token)]:
            with self.subTest(maximum=maximum, lease=lease), self.assertRaises(fence.FenceRejected):
                fence.retry_key(closed, self.attempt, next_lease_token=lease, max_attempts=maximum)

    def test_malformed_persisted_effect_is_never_resolved(self):
        invalid = [
            None,
            {'state': 'completed'},
            {'kind': 'generator', 'request_sha256': 'c' * 64, 'state': 'completed'},
            {'kind': 'generator', 'request_sha256': 'c' * 64, 'state': 'started', 'receipt_sha256': 'd' * 64},
            {'kind': 'foreign-kind', 'request_sha256': 'c' * 64, 'state': 'started'},
            {'kind': 'generator', 'request_sha256': 'c' * 64, 'state': 'completed', 'receipt_sha256': 'invalid'},
        ]
        for effect in invalid:
            document = self.started()
            document['effects'] = {'op-one': effect}
            for operation in (
                lambda: fence.unresolved_effects(document, self.attempt),
                lambda: fence.finish(document, self.attempt, outcome='completed'),
                lambda: fence.callback_allowed(document, self.attempt),
            ):
                with self.subTest(effect=effect), self.assertRaises(fence.FenceRejected):
                    operation()

    def test_phase_and_outcome_corruption_rejects(self):
        for phase, outcome in [
            ('reserved', 'completed'),
            ('started', 'unknown_quarantined'),
            ('closed', None),
            ('closed', 'completed'),
        ]:
            document = self.started()
            document.update(phase=phase, outcome=outcome)
            with self.subTest(phase=phase, outcome=outcome), self.assertRaises(fence.FenceRejected):
                fence.unresolved_effects(document, self.attempt)

    def test_same_request_cannot_be_sent_under_new_operation_id(self):
        with self.assertRaises(fence.FenceRejected):
            fence.start_effect(
                self.effect(), self.attempt, operation_id='op-two', kind='generator', request_sha256='c' * 64
            )

    def test_persisted_boolean_attempt_is_not_integer_attempt(self):
        document = self.started()
        document['key']['attempt'] = True
        with self.assertRaises(fence.FenceRejected):
            fence.callback_allowed(document, self.attempt)

    def test_unhashable_corruption_has_explicit_fence_rejection(self):
        document = self.started()
        document['phase'] = []
        with self.assertRaises(fence.FenceRejected):
            fence.callback_allowed(document, self.attempt)
        document = self.effect()
        document['effects']['op-one']['kind'] = []
        with self.assertRaises(fence.FenceRejected):
            fence.unresolved_effects(document, self.attempt)


if __name__ == '__main__':
    unittest.main()
