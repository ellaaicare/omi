"""Actual candidate transaction functions, synthetic serializable store.

Cloud SDK/credential initialization and transcript decoding are replaced with
small boundary doubles. Admission/eligibility/claim/begin/finish are real source.
No backend, HTTP, provider, real account or captured content is accessed.
"""

import ast
import asyncio
from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import importlib
import importlib.util
import typing
import json
from pathlib import Path
import sys
from types import ModuleType
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()


def pin(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, TypeError):
        return False


def binding(doc, uid, cid):
    if doc.get('uid') != uid or doc.get('id') != cid:
        return None
    return doc.get('test_binding')


class Conflict(RuntimeError):
    pass


class Snapshot:
    def __init__(self, ref, value, version, created):
        self.reference, self.id = ref, ref.id
        self.value, self.version = deepcopy(value), version
        self.exists = value is not None
        self.create_time, self.update_time = created, created

    def to_dict(self):
        return deepcopy(self.value)


class Reference:
    def __init__(self, db, path):
        self.db, self.key, self.id = db, path, path[-1]

    def collection(self, name):
        return Query(self.db, (*self.key, name))

    @property
    def path(self):
        return '/'.join(self.key)

    def get(self, transaction=None, **kwargs):
        if transaction is not None:
            if transaction.writes:
                raise AssertionError('Firestore read after write')
            transaction.reads[self.key] = self.db.versions.get(self.key, 0)
        return Snapshot(
            self,
            self.db.rows.get(self.key),
            self.db.versions.get(self.key, 0),
            self.db.created.get(self.key, NOW - timedelta(hours=2)),
        )


class Query:
    def __init__(self, db, path, filters=(), orders=(), maximum=50, cursor=None):
        self.db, self.key, self.filters, self.orders, self.maximum, self.cursor = (
            db,
            path,
            filters,
            orders,
            maximum,
            cursor,
        )

    def document(self, name):
        return Reference(self.db, (*self.key, name))

    def where(self, *, filter):
        return Query(self.db, self.key, (*self.filters, filter), self.orders, self.maximum, self.cursor)

    def order_by(self, field):
        return Query(self.db, self.key, self.filters, (*self.orders, field), self.maximum, self.cursor)

    def limit(self, count):
        return Query(self.db, self.key, self.filters, self.orders, count, self.cursor)

    def start_after(self, cursor):
        return Query(self.db, self.key, self.filters, self.orders, self.maximum, cursor)

    def select(self, fields):
        return self

    def get(self, transaction=None, **kwargs):
        rows = list(self.stream())
        if transaction is not None:
            for row in rows:
                transaction.reads[row.reference.key] = row.version
        return rows

    def stream(self, **kwargs):
        self.db.streams += 1
        snapshots = []
        for path, row in self.db.rows.items():
            if path[:-1] != self.key:
                continue
            if any(
                (row.get(field) != value if op == '==' else row.get(field) not in value)
                for field, op, value in self.filters
            ):
                continue
            snapshots.append(Reference(self.db, path).get())
        key = lambda snapshot: tuple(snapshot.value.get(field) for field in self.orders) + (snapshot.id,)
        snapshots.sort(key=key)
        if self.cursor:
            snapshots = [s for s in snapshots if key(s) > key(self.cursor)]
        return iter(snapshots[: self.maximum])


class Transaction:
    def __init__(self, db):
        self.db, self.reads, self.writes = db, {}, []

    def update(self, ref, values):
        self.writes.append(('update', ref.key, deepcopy(values)))

    def set(self, ref, values, **kwargs):
        self.writes.append(('set', ref.key, deepcopy(values)))

    def create(self, ref, values):
        self.writes.append(('create', ref.key, deepcopy(values)))

    def commit(self):
        if any(self.db.versions.get(path, 0) != version for path, version in self.reads.items()):
            raise Conflict('transaction conflict')
        for action, path, value in self.writes:
            if action == 'create' and path in self.db.rows:
                raise Conflict('create precondition')
        for action, path, value in self.writes:
            self.db.put(path, {**self.db.rows[path], **value} if action == 'update' else value)
        if self.db.commit_unknown:
            self.db.commit_unknown = False
            raise TimeoutError('commit outcome unknown')


def transactional(fn):
    @wraps(fn)
    def call(transaction, *args, **kwargs):
        result = fn(transaction, *args, **kwargs)
        transaction.commit()
        return result

    return call


class Store:
    def __init__(self):
        self.rows, self.versions, self.created = {}, {}, {}
        self.streams, self.commit_unknown = 0, False

    def collection(self, name):
        return Query(self, (name,))

    def transaction(self):
        return Transaction(self)

    def put(self, path, value, created=None):
        self.rows[path] = deepcopy(value)
        self.versions[path] = self.versions.get(path, 0) + 1
        self.created.setdefault(path, created or NOW - timedelta(hours=2))


@dataclass
class TerminalTransition:
    uid: str
    conversation_id: str
    source_created_at: datetime
    prior_updated_at: datetime
    first_completed_at: datetime
    source_binding: dict
    binding_sha256: str


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


class IntegratedQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = module('database')
        package.__path__ = [str(ROOT / 'candidate/database')]
        native = module(
            'database.normal_native_source_once',
            pin=pin,
            uuid=uuid,
            sha=lambda x: hashlib.sha256(x.encode()).hexdigest(),
            nonempty_transcript=lambda doc: bool(doc.get('transcript_segments')),
        )
        future = module(
            'database.normal_future_source_disposition',
            digest=digest,
            aware=lambda x: isinstance(x, datetime) and x.tzinfo is not None,
            IDENTITY=('job_id', 'uid', 'conversation_id', 'client_interaction_id', 'transcript_sha256'),
            SOURCE_FIELDS=(),
            TerminalTransition=TerminalTransition,
            _worker_job=ContextVar('synthetic_worker', default=None),
        )
        product = module(
            'database.normal_product_capture_provenance',
            binding=binding,
            source_native=lambda doc, uid, cid: doc.get('uid') == uid and doc.get('id') == cid,
        )
        mods = {
            'database': package,
            'google': module('google'),
            'google.cloud': module('google.cloud'),
            'google.cloud.firestore_v1': module(
                'google.cloud.firestore_v1', transactional=transactional, FieldFilter=lambda *args: args
            ),
            future.__name__: future,
            native.__name__: native,
            product.__name__: product,
            'database.normal_source_disposition': module('database.normal_source_disposition'),
            'database.conversations': module(
                'database.conversations', _prepare_conversation_for_read=lambda doc, uid: doc
            ),
            'ella': module('ella'),
            'ella.services': module('ella.services'),
            'ella.services.normal_account_routing': module(
                'ella.services.normal_account_routing', admitted_subject=lambda uid: bool(uid)
            ),
        }
        cls.saved = dict(sys.modules)
        cls.modules = patch.dict(sys.modules, mods)
        cls.modules.start()
        cls.lifecycle = importlib.import_module('database.normal_account_lifecycle')
        cls.activation = importlib.import_module('database.normal_enrichment_activation')
        cls.isolation = importlib.import_module('database.enrichment_job_isolation')
        cls.fence = importlib.import_module('database.enrichment_attempt_fence')
        cls.client = module('database._client', db=None)
        sys.modules['database._client'] = cls.client
        cls.graph = importlib.import_module('database.enrichment_graph_guard')
        cls.future = future
        spec = importlib.util.spec_from_file_location(
            'queue_policy_transition_candidate', ROOT / 'control/queue_policy_transition.py'
        )
        cls.transition = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.transition
        spec.loader.exec_module(cls.transition)

        class ProvisioningError(ValueError):
            def __init__(self, code, **kwargs):
                self.code = code

        sys.modules['ella.services.runtime_errors'] = module(
            'ella.services.runtime_errors', ProvisioningError=ProvisioningError
        )
        hold_tree = ast.parse((ROOT / 'baseline/ella/services/transcript_processing_hold.py').read_text())
        hold_nodes = [
            node
            for node in hold_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name in {'assert_transcript_processing_active', '_read_policy'}
        ]
        hold_scope = {'datetime': datetime, 'timezone': timezone, 'ProvisioningError': ProvisioningError}
        exec(
            compile(ast.Module(body=hold_nodes, type_ignores=[]), '<actual-serving-processing-hold>', 'exec'),
            hold_scope,
        )
        cls.assert_hold = staticmethod(hold_scope['assert_transcript_processing_active'])
        # Use the REAL candidate repository class. Its unrelated legacy module
        # dependencies are excluded through AST, not mocked implementations.
        tree = ast.parse((ROOT / 'candidate/database/hermes_cloud_enrichment_outbox.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'NormalAccountEnrichmentOutbox')

        class Base:
            def __init__(self, db):
                self.db = db

        scope = {
            'FirestoreHermesCloudEnrichmentOutbox': Base,
            'activation': cls.activation,
            'isolation': cls.isolation,
            'COLLECTION': cls.activation.JOBS,
            'FieldFilter': lambda *args: args,
            '_utcnow': lambda: NOW,
            '_aware': lambda x: x,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-candidate-repository>', 'exec'), scope)
        cls.Repository = scope['NormalAccountEnrichmentOutbox']

    @classmethod
    def tearDownClass(cls):
        cls.modules.stop()

    def setUp(self):
        self.db = Store()
        self.graph.db = self.db
        self.client.db = self.db
        self.policy = {
            'version': 4,
            'mode': self.lifecycle.MODE,
            'state': 'active',
            'enrollment_enabled': True,
            'lifecycle_authorization_ref': 'synthetic-authorized-flow',
            'epoch': str(uuid4()),
            'source_completion_boundary': NOW - timedelta(hours=3),
            'active': [],
            'quarantined_tenants': ['old-owner-1', 'old-owner-2'],
            'hour': int(NOW.timestamp()) // 3600,
            'hour_claims': 0,
            'quotas': {**self.activation.DEFAULT_QUOTAS, 'max_attempts': 3},
            'queue_isolation': {'contract': self.isolation.CONTRACT, 'maintenance_revision': 0, 'claims_open': True},
            'queue_unresolved_resources': [],
        }
        self.db.put(self.activation.reference(self.db).key, self.policy)

    def owner(self, uid):
        tenant = {'uid': uid, 'active_job_id': None, 'quarantined_job_id': None, 'additional_quarantined_job_ids': []}
        account = {
            'contract': self.lifecycle.CONTRACT,
            'uid': uid,
            'state': 'active',
            'epoch': str(uuid4()),
            'enabled_at': NOW - timedelta(hours=2),
            'route_sha256': 'a' * 64,
            'account_owner_sha256': 'b' * 64,
            'profile_sha256': 'c' * 64,
            'quarantine_sha256': digest(self.lifecycle.quarantine_image(tenant, uid)),
            'held_preimages': {},
            'excluded_source_ids_sha256': [],
        }
        self.db.put(self.lifecycle.account_ref(self.db, uid).key, account)
        self.db.put(self.activation.tenant_ref(self.db, uid).key, tenant)
        return account

    def job(self, uid, suffix='one'):
        cid, generation = str(uuid4()), str(uuid4())
        row = {
            'job_id': 'job-' + uid + '-' + suffix,
            'uid': uid,
            'conversation_id': cid,
            'client_interaction_id': 'client-' + suffix,
            'transcript_sha256': 'd' * 64,
            'account_routing_contract': 1,
            'admission_epoch': self.policy['epoch'],
            'attempt_count': 0,
            'status': 'pending',
            'dispatch_state': 'never_started',
            'next_attempt_at': NOW - timedelta(minutes=1),
            'lease_token': None,
            'lease_expires_at': None,
            'source_completed_at': NOW - timedelta(minutes=30),
        }
        b = {
            'source_family': 'synthetic',
            'source_sha256': 'e' * 64,
            'source_version': 'synthetic-source-v1',
            'transcript_sha256': row['transcript_sha256'],
            'client_interaction_id': row['client_interaction_id'],
        }
        tag = {
            'contract': self.lifecycle.CONTRACT,
            'account_epoch': self.account(uid)['epoch'],
            'route_sha256': self.account(uid)['route_sha256'],
            'source_created_at': NOW - timedelta(hours=1),
            'first_completed_at': row['source_completed_at'],
            'source_binding': b,
            'binding_sha256': digest(b),
            'capture_generation': generation,
            'capture_owner_sha256': hashlib.sha256(b'owner-token').hexdigest(),
            'marker_id': digest({'uid': uid, 'cid': cid}),
        }
        row[self.lifecycle.TAG] = tag
        source = {
            'id': cid,
            'uid': uid,
            'source': 'synthetic',
            'status': 'completed',
            'test_binding': b,
            'capture_generation': generation,
            'capture_owner_token': 'owner-token',
            'normal_source_completed_at': row['source_completed_at'],
            'transcript_segments': ['fictional'],
        }
        self.db.put(('users', uid, 'conversations', cid), source, created=tag['source_created_at'])
        marker = {k: row[k] for k in self.activation.IDENTITY}
        marker.update(contract=self.lifecycle.CONTRACT, state='prepared', tag=tag, epoch=self.policy['epoch'])
        self.db.put((self.lifecycle.ADMISSIONS, tag['marker_id']), marker)
        self.db.put((self.activation.JOBS, row['job_id']), row)
        return row

    def account(self, uid):
        return self.lifecycle.account_ref(self.db, uid).get().to_dict()

    def claim(self, job, at=NOW):
        return self.activation.claim(
            self.db.transaction(),
            self.db,
            self.db.collection(self.activation.JOBS).document(job['job_id']),
            now=at,
            lease_seconds=900,
        )

    def begin(self, job):
        return self.activation.begin(
            self.db.transaction(),
            self.db,
            self.db.collection(self.activation.JOBS).document(job['job_id']),
            now=NOW,
            lease_token=job['lease_token'],
        )

    def publish_completed(self, job, version):
        path = ('users', job['uid'], 'conversations', job['conversation_id'])
        current = deepcopy(self.db.rows[path])
        current.update(
            active_summary_version_id=version,
            summary_writeback_receipt={
                'status': 'completed',
                'canonical_status': 'completed',
                'summary_source': 'hermes',
                'summary_kind': 'hermes_enriched',
                'source_sha256': 'e' * 64,
                'source_version': 'synthetic-source-v1',
                'active_summary_version_id': version,
            },
            enrichment_state={'status': 'writeback_applied', 'canonical_status': 'completed', 'pending': False},
        )
        self.db.put(path, current)

    def finish(self, job, outcome='unknown', error='synthetic_unknown', at=NOW, **kwargs):
        return self.activation.finish(
            self.db.transaction(),
            self.db,
            self.db.collection(self.activation.JOBS).document(job['job_id']),
            now=at,
            lease_token=job['lease_token'],
            outcome=outcome,
            error_code=error,
            **kwargs,
        )

    def test_v4_real_policy_and_complete_transaction_path(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.assertTrue(self.begin(job))
        version = str(uuid4())
        self.publish_completed(job, version)
        self.assertTrue(
            self.finish(
                job,
                outcome='success',
                receipt={
                    'content_free': True,
                    'runtime_interaction_id': 'runtime',
                    'active_summary_version_id': version,
                },
            )
        )
        stored = self.db.rows[(self.activation.JOBS, job['job_id'])]
        self.assertEqual(stored['status'], 'completed')
        self.assertIsNone(stored['lease_token'])

    def test_historical_quarantines_do_not_consume_active_capacity(self):
        self.owner('one')
        self.policy['quotas']['max_quarantined_tenants'] = 2
        self.db.put(self.activation.reference(self.db).key, self.policy)
        self.assertIsNotNone(self.claim(self.job('one')))

    def test_new_quarantine_preserves_account_and_other_owner_progress(self):
        self.owner('one')
        self.owner('two')
        job = self.claim(self.job('one'))
        self.assertTrue(self.begin(job))
        self.assertTrue(self.finish(job))
        policy = self.activation.read_policy(self.db)
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        self.assertIsNotNone(self.lifecycle.read_account(None, self.db, 'one', tenant, NOW, policy=policy))
        self.assertEqual(policy['quarantined_tenants'], self.policy['quarantined_tenants'])
        self.assertEqual(len(policy['queue_unresolved_resources']), 1)
        self.assertIsNone(self.claim(self.job('one', 'next')))
        self.assertIsNotNone(self.claim(self.job('two')))

    def test_unknown_resources_still_count_toward_finite_global_capacity(self):
        self.owner('one')
        self.owner('two')
        self.policy['quotas']['max_concurrency'] = 1
        self.db.put(self.activation.reference(self.db).key, self.policy)
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        self.assertIsNone(self.claim(self.job('two')))

    def test_actual_preflight_retry_marker_and_old_attempt_aba(self):
        self.owner('one')
        first = self.claim(self.job('one'))
        self.assertTrue(self.finish(first, outcome='preflight', error='account_runtime_not_ready'))
        old = self.db.rows[(self.activation.JOBS, first['job_id'])]
        second = self.claim(old, at=NOW + timedelta(seconds=61))
        self.assertEqual(second['attempt_count'], 2)
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        self.assertFalse(self.begin(first))
        self.assertFalse(self.finish(first))
        self.assertTrue(self.begin(second))

    def test_unknown_never_rearmed_by_editing_status_and_marker(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        row = deepcopy(self.db.rows[(self.activation.JOBS, job['job_id'])])
        row.update(status='pending', attempt_count=0, dispatch_state='never_started')
        self.db.put((self.activation.JOBS, job['job_id']), row)
        marker = self.db.rows[(self.lifecycle.ADMISSIONS, row[self.lifecycle.TAG]['marker_id'])]
        marker.update(state='prepared', lease_token=None)
        self.assertIsNone(self.claim(row))

    def test_maintenance_parks_reserved_attempt_without_capture_dependency(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        policy = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(policy),
            expected_revision=0,
            claims_open=False,
            now=NOW,
        )
        self.assertFalse(self.begin(job))
        self.assertTrue(self.finish(job, outcome='preflight', error='queue_maintenance'))
        row = self.db.rows[(self.activation.JOBS, job['job_id'])]
        self.assertEqual(row['status'], 'retryable')
        self.assertEqual(row['preflight_retry_count'], 0)
        self.assertEqual(set(self.db.rows) & {('capture',)}, set())
        policy = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(policy),
            expected_revision=1,
            claims_open=True,
            now=NOW,
        )
        self.assertIsNotNone(self.claim(row, at=NOW + timedelta(seconds=61)))

    def test_stale_maintenance_cas_cannot_reopen_newer_revision(self):
        policy = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(policy),
            expected_revision=0,
            claims_open=False,
            now=NOW,
        )
        with self.assertRaises(self.fence.FenceRejected):
            self.activation.set_worker_claims(
                self.db.transaction(),
                self.db,
                expected_policy_sha256=digest(policy),
                expected_revision=0,
                claims_open=True,
                now=NOW,
            )

    def test_graph_cas_read_conflicts_with_concurrent_job_close(self):
        self.owner('one')
        claimed = self.claim(self.job('one'))
        self.begin(claimed)
        transaction = self.db.transaction()
        self.graph.require_in_transaction(transaction, self.db, claimed, 'one', claimed['conversation_id'])
        target = self.db.collection('synthetic_writeback').document('one')
        transaction.create(target, {'status': 'candidate'})
        self.finish(claimed)
        with self.assertRaises(Conflict):
            transaction.commit()
        self.assertNotIn(target.key, self.db.rows)
        with self.assertRaises(self.fence.FenceRejected):
            self.graph.require_in_transaction(
                self.db.transaction(), self.db, claimed, 'one', claimed['conversation_id']
            )

    def test_foreign_owner_route_and_marker_drift_fail_closed(self):
        self.owner('one')
        self.owner('two')
        for change in ('owner', 'route', 'marker'):
            job = self.job('one', change)
            if change == 'owner':
                job['uid'] = 'two'
            if change == 'route':
                job[self.lifecycle.TAG]['route_sha256'] = 'f' * 64
            if change == 'marker':
                self.db.rows[(self.lifecycle.ADMISSIONS, job[self.lifecycle.TAG]['marker_id'])]['uid'] = 'two'
            self.db.put((self.activation.JOBS, job['job_id']), job)
            self.assertIsNone(self.claim(job))

    def test_repository_scan_passes_more_than_one_ineligible_page(self):
        self.owner('one')
        self.owner('two')
        for index in range(55):
            job = self.job('one', f'{index:03}')
            job['next_attempt_at'] = NOW - timedelta(hours=1)
            job['normal_account_lifecycle_admission']['route_sha256'] = 'f' * 64
            self.db.put((self.activation.JOBS, job['job_id']), job)
        target = self.job('two', 'healthy')
        result = self.Repository(self.db).claim_next(lease_seconds=900)
        self.assertEqual(result['job_id'], target['job_id'])
        self.assertEqual(self.db.streams, 2)

    def test_commit_unknown_does_not_authorize_second_claim(self):
        self.owner('one')
        job = self.job('one')
        self.db.commit_unknown = True
        with self.assertRaises(TimeoutError):
            self.claim(job)
        self.assertIsNone(self.claim(job))
        self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])]['attempt_count'], 1)

    def test_actual_serving_hold_allows_inflight_during_claims_maintenance(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        policy = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(policy),
            expected_revision=0,
            claims_open=False,
            now=NOW,
        )
        self.assert_hold('one')
        state = self.lifecycle.processing_status(self.db, 'one', now=NOW)
        self.assertEqual(state['state'], 'processing')
        self.assertEqual(state['queue_state'], 'maintenance')
        self.graph.require_in_transaction(self.db.transaction(), self.db, job, 'one', job['conversation_id'])
        version = str(uuid4())
        self.publish_completed(job, version)
        self.assertTrue(
            self.finish(
                job,
                outcome='success',
                receipt={
                    'content_free': True,
                    'runtime_interaction_id': 'runtime',
                    'active_summary_version_id': version,
                },
            )
        )

    def test_retry_limit_blocks_only_job_and_does_not_consume_resource_capacity(self):
        self.owner('one')
        source = self.job('one')
        for n in range(3):
            at = NOW + timedelta(seconds=n * 61)
            attempt = self.claim(source, at=at)
            self.assertIsNotNone(attempt)
            self.finish(attempt, outcome='preflight', error='account_model_unavailable', at=at)
        row = self.db.rows[(self.activation.JOBS, source['job_id'])]
        self.assertEqual(row['status'], 'blocked')
        self.assertEqual(row['attempt_count'], 3)
        self.assertIsNone(self.claim(source, at=NOW + timedelta(seconds=240)))
        self.assertEqual(self.activation.read_policy(self.db)['queue_unresolved_resources'], [])
        self.assertIsNotNone(self.claim(self.job('one', 'different')))

    def test_source_admission_and_maintenance_do_not_require_active_capture_stop(self):
        self.owner('one')
        active_path = ('capture_ownership', 'one')
        active = {
            'state': 'active',
            'generation': str(uuid4()),
            'lease_expires_at': NOW + timedelta(hours=1),
            'last_audio_at': NOW,
            'received_frames': 42,
        }
        self.db.put(active_path, active)
        source = self.job('one', 'new-completed')
        tag = source[self.lifecycle.TAG]
        cid = source['conversation_id']
        source_path = ('users', 'one', 'conversations', cid)
        doc = self.db.rows[source_path]
        doc.update(status='in_progress')
        doc.pop('normal_source_completed_at')
        del self.db.rows[(self.lifecycle.ADMISSIONS, tag['marker_id'])]
        transition = TerminalTransition(
            'one',
            cid,
            tag['source_created_at'],
            tag['source_created_at'],
            NOW,
            tag['source_binding'],
            tag['binding_sha256'],
        )
        policy = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(policy),
            expected_revision=0,
            claims_open=False,
            now=NOW,
        )
        policy = self.activation.read_policy(self.db)
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        transaction = self.db.transaction()
        self.assertIsNotNone(self.lifecycle.admission(transaction, self.db, source, policy, tenant, NOW, transition))
        self.assertNotIn(active_path, transaction.reads)
        self.assertEqual(self.db.rows[active_path], active)

    def test_v3_keeps_original_history_hold_and_unknown_v4_fails_closed(self):
        self.owner('one')
        job = self.job('one')
        p = deepcopy(self.policy)
        p['version'] = 3
        p['quotas']['max_attempts'] = 1
        self.db.put(self.activation.reference(self.db).key, p)
        self.assertTrue(self.lifecycle.policy_shape(p))
        tenant_path = self.activation.tenant_ref(self.db, 'one').key
        self.db.rows[tenant_path]['quarantined_job_id'] = 'new-unsealed'
        self.assertIsNone(self.lifecycle.read_account(None, self.db, 'one', self.db.rows[tenant_path], NOW, policy=p))
        self.assertIsNone(self.claim(job))
        for change in ('unknown_contract', 'missing_control', 'bool_revision'):
            p = deepcopy(self.policy)
            if change == 'unknown_contract':
                p['queue_isolation']['contract'] = 'unknown'
            if change == 'missing_control':
                del p['queue_isolation']
            if change == 'bool_revision':
                p['queue_isolation']['maintenance_revision'] = True
            self.db.put(self.activation.reference(self.db).key, p)
            self.assertFalse(self.lifecycle.policy_shape(p))
            self.assertIsNone(self.claim(job))

    def test_real_source_cas_function_rejects_guard_before_any_write(self):
        tree = ast.parse((ROOT / 'candidate/database/conversations.py').read_text())
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == '_update_conversation_with_builder_transaction'
        )
        scope = {
            'Optional': __import__('typing').Optional,
            '_prepare_conversation_for_read': lambda doc, uid: doc,
            '_prepare_conversation_for_write': lambda doc, uid, level: doc,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-source-cas>', 'exec'), scope)
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        token = self.future._worker_job.set(job)
        try:
            transaction = self.db.transaction()
            with self.assertRaises(self.fence.FenceRejected):
                scope[node.name](
                    transaction,
                    self.db.collection('users')
                    .document('one')
                    .collection('conversations')
                    .document(job['conversation_id']),
                    'one',
                    lambda doc: ({'overview': 'fictional'}, {}),
                    transaction_guard=self.graph.make_cas_guard('one', job['conversation_id']),
                )
            self.assertEqual(transaction.writes, [])
        finally:
            self.future._worker_job.reset(token)

    def test_real_model_entry_cannot_send_after_graph_closed(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        source = ast.parse((ROOT / 'candidate/ella/services/semantic_self_hosted_runtime.py').read_text())
        method = next(
            n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'SelfHostedSemanticRuntimeBridge'
        )

        async def await_stage(name, awaitable):
            return await awaitable

        def reject(code):
            raise ValueError(code)

        scope = {
            '_timing': SimpleNamespace(stage=lambda name: nullcontext(), await_stage=await_stage),
            'SELF_HOSTED_CHANNELS': {'generator': 'generator'},
            'ROLE_POLICIES': {'generator': 'policy'},
            'copy': __import__('copy'),
            'json': json,
            'MAX_REQUEST_BYTES': 100000,
            'digest': digest,
            'asdict': asdict,
            'datetime': datetime,
            'timezone': timezone,
            'asyncio': asyncio,
            'require_queue_graph': self.graph.require_current,
            '_reject': reject,
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<actual-model-entry>', 'exec'), scope)
        cls = scope['SelfHostedSemanticRuntimeBridge']
        bridge = object.__new__(cls)

        @dataclass
        class Binding:
            uid: str
            conversation_id: str

        bridge.binding = Binding('one', job['conversation_id'])
        bridge.timeout_seconds = 45

        async def authority(*args):
            return SimpleNamespace(), 'synthetic-identity'

        bridge._authority = authority

        class NoSend:
            calls = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError('unexpected send')

        client = NoSend()
        bridge._deps = SimpleNamespace(client_factory=lambda **kwargs: client)
        token = self.future._worker_job.set(job)
        try:
            with self.assertRaises(self.fence.FenceRejected):
                asyncio.run(bridge(role='generator', request={'policy': 'policy', 'instructions': 'fictional'}))
            self.assertEqual(client.calls, 0)
        finally:
            self.future._worker_job.reset(token)

    def test_malformed_predecessor_fence_cannot_be_overwritten_by_retry(self):
        self.owner('one')
        for change in ('effect', 'phase', 'identity', 'operation_lease'):
            source = self.job('one', change)
            first = self.claim(source)
            self.finish(first, outcome='preflight', error='account_runtime_not_ready')
            current = deepcopy(self.db.rows[(self.activation.JOBS, source['job_id'])])
            prior = current[self.isolation.FENCE_FIELD]
            if change == 'effect':
                prior['effects']['late'] = {'kind': 'generator', 'request_sha256': 'a' * 64, 'state': 'started'}
            if change == 'phase':
                prior['phase'] = 'started'
            if change == 'identity':
                prior['key']['transcript_sha256'] = 'f' * 64
            if change == 'operation_lease':
                current['operation']['lease_token'] = 'other-lease'
            self.db.put((self.activation.JOBS, source['job_id']), current)
            self.assertIsNone(self.claim(current, at=NOW + timedelta(seconds=61)))
            self.assertEqual(self.db.rows[(self.activation.JOBS, source['job_id'])]['attempt_count'], 1)

    def test_global_unresolved_resource_blocks_owner_even_if_tenant_flag_missing(self):
        self.owner('one')
        self.owner('two')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        path = self.activation.tenant_ref(self.db, 'one').key
        self.db.rows[path].pop('queue_resource_hold')
        self.assertIsNone(self.claim(self.job('one', 'next')))
        self.assertIsNotNone(self.claim(self.job('two')))

    def test_graph_guard_rejects_unknown_lifecycle_versions_with_or_without_context(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        for change in ('future_version', 'missing_version', 'invalid_control'):
            p = self.activation.read_policy(self.db)
            if change == 'future_version':
                p['version'] = 5
            if change == 'missing_version':
                p.pop('version', None)
            if change == 'invalid_control':
                p['version'] = 4
                p['queue_isolation'] = {}
            self.db.put(self.activation.reference(self.db).key, p)
            for expected in (job, None):
                with self.subTest(change=change, expected=expected is None), self.assertRaises(
                    self.fence.FenceRejected
                ):
                    self.graph.require_in_transaction(
                        self.db.transaction(), self.db, expected, 'one', job['conversation_id']
                    )

    def test_worker_success_without_actual_canonical_publication_stays_unknown(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.assertTrue(
            self.finish(
                job,
                outcome='success',
                receipt={
                    'content_free': True,
                    'runtime_interaction_id': 'runtime',
                    'active_summary_version_id': str(uuid4()),
                },
            )
        )
        current = self.db.rows[(self.activation.JOBS, job['job_id'])]
        self.assertEqual(current['status'], 'quarantined')
        self.assertEqual(current['last_error_code'], 'normal_canonical_completion_unconfirmed')
        self.assertEqual(len(self.activation.read_policy(self.db)['queue_unresolved_resources']), 1)

    def publication_fixture(self, job, close_after_initial=False):

        tree = ast.parse((ROOT / 'candidate/ella/services/summary_writeback.py').read_text())
        wanted = {
            'write_conversation_summary_cas',
            '_sha256_json',
            '_active_summary_version_sha256',
            '_canonical_source_version',
            '_publication_post_image_sha256',
            '_assert_canonical_publication_sha256',
            '_receipt_matches_request',
            '_receipt_matches_current_post_image',
            '_public_operation_receipt',
            '_cas_result',
            '_reconcile_superseded_canonical_publication',
        }
        scope = {
            'asyncio': asyncio,
            'copy': __import__('copy'),
            'hashlib': hashlib,
            'json': json,
            'datetime': datetime,
            'timezone': timezone,
            'Any': typing.Any,
            'Callable': typing.Callable,
            'Optional': typing.Optional,
            'Awaitable': typing.Awaitable,
            'write_omi_canonical_event': lambda *a, **k: None,
            'sanitize_summary_update': lambda **kw: SimpleNamespace(**kw, warnings=[]),
            'make_cas_guard': self.graph.make_cas_guard,
            'require_queue_graph': self.graph.require_current,
            'QueueAttemptFenceRejected': self.fence.FenceRejected,
            'logger': SimpleNamespace(error=lambda *a, **k: None),
        }
        for node in tree.body:
            if isinstance(node, ast.Assign):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                for name in node.targets:
                    if isinstance(name, ast.Name):
                        scope[name.id] = value
        for name in (
            'CanonicalSummaryOperationConflictError',
            'CanonicalSummaryReconciliationPendingError',
            'CanonicalConversationSourceMismatchError',
            'SemanticPublicationRejectedError',
            'ConversationSummaryOutcomeUnknownError',
            'ConversationSummaryNotFoundError',
            'CanonicalSummaryDependencyUnavailableError',
            'InvalidConversationSummaryCategoryError',
            '_CanonicalSummaryRepairRetry',
        ):
            scope[name] = type(name, (ValueError,), {})

        def canonical_source(**kwargs):
            doc = kwargs['conversation']
            return {
                'uid': kwargs['uid'],
                'cid': kwargs['conversation_id'],
                'started_at': doc.get('started_at'),
                'finished_at': doc.get('finished_at'),
                'transcript_segments': doc.get('transcript_segments'),
            }

        scope['canonical_source_from_conversation'] = canonical_source
        scope['canonical_source_sha256'] = digest
        scope['_active_summary_version'] = lambda doc: next(
            (v for v in doc.get('summary_versions', []) if v['id'] == doc.get('active_summary_version_id')), None
        )
        selected = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
        ]
        exec(
            compile(ast.Module(body=selected, type_ignores=[]), '<actual-canonical-continuation-functions>', 'exec'),
            scope,
        )
        dbtree = ast.parse((ROOT / 'candidate/database/conversations.py').read_text())
        node = next(
            n
            for n in dbtree.body
            if isinstance(n, ast.FunctionDef) and n.name == '_update_conversation_with_builder_transaction'
        )
        dbscope = {
            'Optional': typing.Optional,
            '_prepare_conversation_for_read': lambda doc, uid: doc,
            '_prepare_conversation_for_write': lambda data, uid, level: data,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-source-cas>', 'exec'), dbscope)
        initial_calls = []

        def update(uid, cid, builder, correction_id=None, transaction_guard=None):
            tx = self.db.transaction()
            ref = self.db.collection('users').document(uid).collection('conversations').document(cid)
            result = dbscope[node.name](
                tx, ref, uid, builder, correction_id=correction_id, transaction_guard=transaction_guard
            )
            tx.commit()
            if close_after_initial and not initial_calls:
                initial_calls.append(True)
                self.finish(job)
            return result

        scope['conversations_db'] = SimpleNamespace(
            update_conversation_with_builder=update,
            get_conversation=lambda uid, cid: deepcopy(self.db.rows[('users', uid, 'conversations', cid)]),
        )
        path = ('users', job['uid'], 'conversations', job['conversation_id'])
        doc = deepcopy(self.db.rows[path])
        version = str(uuid4())
        token = 'accepted-operation'
        payload = 'a' * 64
        doc.update(
            active_summary_version_id=version,
            structured={'title': 'Fictional fixture'},
            summary_versions=[{'id': version, 'source': 'hermes', 'kind': 'hermes_enriched'}],
            started_at='2026-10-24T00:00:00Z',
            finished_at='2026-10-24T00:01:00Z',
            enrichment_state={
                'status': 'writeback_pending_canonical',
                'pending': True,
                'canonical_status': 'pending',
                'source': 'hermes',
                'kind': 'hermes_enriched',
                'trace_id': token,
            },
        )
        source_hash = digest(canonical_source(uid=job['uid'], conversation_id=job['conversation_id'], conversation=doc))
        receipt = {
            'token': token,
            'payload_sha256': payload,
            'source_sha256': source_hash,
            'source_version': doc['finished_at'],
            'status': scope['SUMMARY_WRITEBACK_PENDING'],
            'canonical_status': 'pending',
            'created_at': NOW,
            'active_summary_version_id': version,
            'active_summary_version_sha256': scope['_active_summary_version_sha256'](doc),
            'canonical_source_post_image_sha256': source_hash,
            'post_image_sha256': scope['_publication_post_image_sha256'](doc),
            'publication_sequence': 1,
        }
        confirmed = {
            **doc['enrichment_state'],
            'status': 'writeback_applied',
            'pending': False,
            'canonical_status': 'completed',
            'error': None,
            'updated_at': NOW,
        }
        receipt['completed_post_image_sha256'] = scope['_publication_post_image_sha256'](
            {**doc, 'enrichment_state': confirmed}
        )
        doc[scope['SUMMARY_WRITEBACK_RECEIPT_FIELD']] = receipt
        doc[scope['CANONICAL_SUMMARY_PUBLICATION_SHA256_FIELD']] = receipt['post_image_sha256']
        self.db.put(path, doc)

        def forbidden_new_admission(current):
            raise AssertionError('new summary admission after graph close')

        arguments = {
            'uid': job['uid'],
            'conversation_id': job['conversation_id'],
            'operation_token': token,
            'payload_sha256': payload,
            'expected_canonical_source_sha256': source_hash,
            'source_version': doc['finished_at'],
            'title': 'Fictional fixture',
            'semantic_publication_authority_guard': forbidden_new_admission,
            'summary_source': 'hermes',
            'summary_kind': 'hermes_enriched',
        }
        return scope, arguments, path, version

    def test_only_already_accepted_pending_publication_can_complete_after_unknown_close(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        scope, args, path, version = self.publication_fixture(job, close_after_initial=True)
        writes = []

        def canonical_writer(uid, document, **kwargs):
            writes.append((uid, document['active_summary_version_id']))
            return {'ok': True}

        token = self.future._worker_job.set(job)
        try:
            result = asyncio.run(scope['write_conversation_summary_cas'](**args, canonical_writer=canonical_writer))
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(writes, [('one', version)])
            self.assertEqual(self.db.rows[path]['active_summary_version_id'], version)
            self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])]['status'], 'quarantined')
            self.assertEqual(len(self.activation.read_policy(self.db)['queue_unresolved_resources']), 1)
        finally:
            self.future._worker_job.reset(token)

    def test_accepted_continuation_rejects_different_token_payload_or_changed_current_image(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        scope, args, path, version = self.publication_fixture(job)
        self.finish(job)
        original = deepcopy(self.db.rows[path])
        calls = []

        def writer(*a, **k):
            calls.append(1)
            return {'ok': True}

        token = self.future._worker_job.set(job)
        try:
            for mutation in ('same_pending', 'token', 'payload', 'current_version', 'transcript'):
                self.db.put(path, original)
                current = deepcopy(args)
                if mutation == 'token':
                    current['operation_token'] = 'different'
                if mutation == 'payload':
                    current['payload_sha256'] = 'b' * 64
                if mutation == 'current_version':
                    self.db.rows[path]['active_summary_version_id'] = 'newer-version'
                if mutation == 'transcript':
                    self.db.rows[path]['transcript_segments'] = ['changed fictional text']
                with self.subTest(mutation=mutation), self.assertRaises((ValueError, self.fence.FenceRejected)):
                    asyncio.run(scope['write_conversation_summary_cas'](**current, canonical_writer=writer))
                self.assertEqual(calls, [])
                self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])]['status'], 'quarantined')
        finally:
            self.future._worker_job.reset(token)

    def test_accepted_canonical_send_cannot_finalize_over_new_current_version(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        scope, args, path, version = self.publication_fixture(job, close_after_initial=True)
        calls = []

        def writer(*a, **k):
            calls.append(1)
            self.db.rows[path]['active_summary_version_id'] = 'newer-version'
            return {'ok': True}

        token = self.future._worker_job.set(job)
        try:
            with self.assertRaises(self.fence.FenceRejected):
                asyncio.run(scope['write_conversation_summary_cas'](**args, canonical_writer=writer))
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.db.rows[path]['active_summary_version_id'], 'newer-version')
            self.assertEqual(self.db.rows[path]['summary_writeback_receipt']['status'], 'pending_reconciliation')
        finally:
            self.future._worker_job.reset(token)

    def migration_fixture(self, terminal=True):
        account = self.owner('one')
        job = self.job('one', 'legacy')
        job.update(
            status='quarantined', dispatch_state='unknown_quarantined', operation={'lease_token': 'old-owned-lease'}
        )
        self.db.put((self.activation.JOBS, job['job_id']), job)
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        tenant['quarantined_job_id'] = job['job_id']
        self.db.put(self.activation.tenant_ref(self.db, 'one').key, tenant)
        old = deepcopy(self.policy)
        old.update(version=3, quarantined_tenants=['one'])
        old['quotas']['max_attempts'] = 1
        old.pop('queue_isolation')
        old.pop('queue_unresolved_resources')
        self.db.put(self.activation.reference(self.db).key, old)
        route = self.lifecycle.VerifiedRoute(
            'one', account['route_sha256'], account['account_owner_sha256'], account['profile_sha256']
        )
        proof = self.transition.ReviewedOwnerSnapshot(
            route,
            digest(account),
            digest(tenant),
            {job['job_id']: digest(job)},
            {job['job_id']: 'a' * 64} if terminal else {},
        )
        return old, proof, job

    def test_migration_changes_only_policy_and_archive_with_active_capture_untouched(self):
        old, proof, job = self.migration_fixture()
        active_path = ('capture_ownership', 'one')
        self.db.put(active_path, {'state': 'active', 'generation': str(uuid4()), 'frames': 42})
        before = deepcopy(self.db.rows)
        result = self.transition.upgrade(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(old),
            owners=(proof,),
            authorization_ref=old['lifecycle_authorization_ref'],
            now=NOW,
        )
        current = self.activation.read_policy(self.db)
        self.assertEqual(result['source_job_account_tenant_writes'], 0)
        self.assertEqual(current['version'], 4)
        self.assertFalse(current['queue_isolation']['claims_open'])
        self.assertEqual(current['epoch'], old['epoch'])
        self.assertEqual(current['source_completion_boundary'], old['source_completion_boundary'])
        for path, row in before.items():
            if path != self.activation.reference(self.db).key:
                self.assertEqual(self.db.rows[path], row)
        self.assertEqual(len(self.db.rows) - len(before), 1)

    def test_migration_carries_unsettled_resource_without_clearing_quarantine(self):
        old, proof, job = self.migration_fixture(terminal=False)
        self.transition.upgrade(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(old),
            owners=(proof,),
            authorization_ref=old['lifecycle_authorization_ref'],
            now=NOW,
        )
        current = self.activation.read_policy(self.db)
        self.assertEqual(current['queue_unresolved_resources'][0]['job_id'], job['job_id'])
        self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])]['status'], 'quarantined')
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(current),
            expected_revision=0,
            claims_open=True,
            now=NOW,
        )
        self.assertIsNone(self.claim(self.job('one', 'future')))

    def test_migration_refuses_active_worker_stale_hash_foreign_route_or_changed_hold(self):
        for change in ('worker', 'policy_hash', 'route', 'hold'):
            self.setUp()
            old, proof, job = self.migration_fixture()
            expected = digest(old)
            if change == 'worker':
                old['active'] = [{'uid': 'one', 'job_id': 'active', 'lease_token': 'active-lease'}]
                self.db.put(self.activation.reference(self.db).key, old)
                expected = digest(old)
            if change == 'policy_hash':
                expected = 'f' * 64
            if change == 'route':
                proof = self.transition.ReviewedOwnerSnapshot(
                    self.lifecycle.VerifiedRoute('foreign', 'a' * 64, 'b' * 64, 'c' * 64),
                    proof.account_sha256,
                    proof.tenant_sha256,
                    proof.held_preimages,
                    proof.terminal_resource_receipts,
                )
            if change == 'hold':
                self.db.rows[(self.activation.JOBS, job['job_id'])]['last_error_code'] = 'changed'
            before = deepcopy(self.db.rows)
            with self.subTest(change=change), self.assertRaises((self.fence.FenceRejected, ValueError)):
                self.transition.upgrade(
                    self.db.transaction(),
                    self.db,
                    expected_policy_sha256=expected,
                    owners=(proof,),
                    authorization_ref=old['lifecycle_authorization_ref'],
                    now=NOW,
                )
            self.assertEqual(self.db.rows, before)

    def test_pristine_migration_rollback_preserves_sources_and_refuses_after_open(self):
        old, proof, job = self.migration_fixture()
        result = self.transition.upgrade(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(old),
            owners=(proof,),
            authorization_ref=old['lifecycle_authorization_ref'],
            now=NOW,
        )
        current = self.activation.read_policy(self.db)
        self.transition.rollback_before_open(
            self.db.transaction(), self.db, archive_id=digest(old), expected_policy_sha256=digest(current), now=NOW
        )
        self.assertEqual(self.activation.read_policy(self.db), old)
        self.setUp()
        old, proof, job = self.migration_fixture()
        self.transition.upgrade(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(old),
            owners=(proof,),
            authorization_ref=old['lifecycle_authorization_ref'],
            now=NOW,
        )
        current = self.activation.read_policy(self.db)
        self.activation.set_worker_claims(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(current),
            expected_revision=0,
            claims_open=True,
            now=NOW,
        )
        with self.assertRaises(self.fence.FenceRejected):
            self.transition.rollback_before_open(
                self.db.transaction(),
                self.db,
                archive_id=digest(old),
                expected_policy_sha256=digest(self.activation.read_policy(self.db)),
                now=NOW,
            )

    def settlement_fixture(self):
        account = self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        self.finish(job)
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        tombstone = self.isolation.tombstone_ref(self.db, job).get().to_dict()
        proof = self.transition.ReviewedResourceSettlement(
            self.lifecycle.VerifiedRoute(
                'one', account['route_sha256'], account['account_owner_sha256'], account['profile_sha256']
            ),
            job['job_id'],
            digest(tombstone),
            digest(tenant),
            'a' * 64,
            digest(account),
            digest(self.db.rows[(self.activation.JOBS, job['job_id'])]),
        )
        return job, proof, tombstone

    def test_reviewed_resource_settlement_unblocks_future_job_without_replaying_quarantine(self):
        job, proof, tombstone = self.settlement_fixture()
        before = deepcopy(self.db.rows[(self.activation.JOBS, job['job_id'])])
        result = self.transition.settle_resource(
            self.db.transaction(),
            self.db,
            proof=proof,
            expected_policy_sha256=digest(self.activation.read_policy(self.db)),
            now=NOW,
        )
        self.assertEqual(result['job_writes'], 0)
        self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])], before)
        self.assertEqual(self.isolation.tombstone_ref(self.db, job).get().to_dict(), tombstone)
        self.assertEqual(self.activation.read_policy(self.db)['queue_unresolved_resources'], [])
        self.assertIsNotNone(self.claim(self.job('one', 'fresh-after-settlement')))

    def test_resource_settlement_rejects_foreign_route_or_changed_tombstone(self):
        for change in ('route', 'tombstone', 'missing_proof', 'missing_tenant_hold'):
            self.setUp()
            job, proof, tombstone = self.settlement_fixture()
            if change == 'route':
                proof = self.transition.ReviewedResourceSettlement(
                    self.lifecycle.VerifiedRoute('one', 'f' * 64, 'b' * 64, 'c' * 64),
                    proof.job_id,
                    proof.tombstone_sha256,
                    proof.tenant_sha256,
                    proof.terminal_receipt_sha256,
                    proof.account_sha256,
                    proof.job_sha256,
                )
            if change == 'tombstone':
                self.db.rows[self.isolation.tombstone_ref(self.db, job).key]['outcome'] = 'changed'
            if change == 'missing_proof':
                proof = self.transition.ReviewedResourceSettlement(
                    proof.route,
                    proof.job_id,
                    proof.tombstone_sha256,
                    proof.tenant_sha256,
                    '',
                    proof.account_sha256,
                    proof.job_sha256,
                )
            if change == 'missing_tenant_hold':
                self.db.rows[self.activation.tenant_ref(self.db, 'one').key].pop('queue_resource_hold')
            before = deepcopy(self.db.rows)
            with self.subTest(change=change), self.assertRaises(self.fence.FenceRejected):
                self.transition.settle_resource(
                    self.db.transaction(),
                    self.db,
                    proof=proof,
                    expected_policy_sha256=digest(self.activation.read_policy(self.db)),
                    now=NOW,
                )
            self.assertEqual(self.db.rows, before)

    def test_settlement_stale_attempt_or_lease_cannot_clear_current_resource(self):
        for change in ('attempt', 'operation_lease', 'fence'):
            self.setUp()
            job, proof, tombstone = self.settlement_fixture()
            row = self.db.rows[(self.activation.JOBS, job['job_id'])]
            if change == 'attempt':
                row['attempt_count'] += 1
            if change == 'operation_lease':
                row['operation']['lease_token'] = 'new-lease'
            if change == 'fence':
                row[self.isolation.FENCE_FIELD]['key']['attempt'] += 1
            before = deepcopy(self.db.rows)
            with self.subTest(change=change), self.assertRaises(self.fence.FenceRejected):
                self.transition.settle_resource(
                    self.db.transaction(),
                    self.db,
                    proof=proof,
                    expected_policy_sha256=digest(self.activation.read_policy(self.db)),
                    now=NOW,
                )
            self.assertEqual(self.db.rows, before)

    def test_migrated_legacy_resource_has_exact_archive_anchored_settlement(self):
        old, ownerproof, job = self.migration_fixture(terminal=False)
        self.transition.upgrade(
            self.db.transaction(),
            self.db,
            expected_policy_sha256=digest(old),
            owners=(ownerproof,),
            authorization_ref=old['lifecycle_authorization_ref'],
            now=NOW,
        )
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        account = self.account('one')
        before = deepcopy(self.db.rows[(self.activation.JOBS, job['job_id'])])
        proof = self.transition.ReviewedResourceSettlement(
            ownerproof.route,
            job['job_id'],
            None,
            digest(tenant),
            'a' * 64,
            digest(account),
            digest(before),
            migration_archive_id=digest(old),
        )
        result = self.transition.settle_resource(
            self.db.transaction(),
            self.db,
            proof=proof,
            expected_policy_sha256=digest(self.activation.read_policy(self.db)),
            now=NOW,
        )
        self.assertEqual(result['job_writes'], 0)
        self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])], before)
        self.assertEqual(self.activation.tenant_ref(self.db, 'one').get().to_dict(), tenant)
        self.assertEqual(self.activation.read_policy(self.db)['queue_unresolved_resources'], [])
        self.assertFalse(self.isolation.tombstone_ref(self.db, job).get().exists)

    def test_legacy_settlement_rejects_wrong_archive_or_changed_job(self):
        for change in ('archive', 'job'):
            self.setUp()
            old, ownerproof, job = self.migration_fixture(terminal=False)
            self.transition.upgrade(
                self.db.transaction(),
                self.db,
                expected_policy_sha256=digest(old),
                owners=(ownerproof,),
                authorization_ref=old['lifecycle_authorization_ref'],
                now=NOW,
            )
            tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
            row = self.db.rows[(self.activation.JOBS, job['job_id'])]
            if change == 'job':
                row['attempt_count'] += 1
            proof = self.transition.ReviewedResourceSettlement(
                ownerproof.route,
                job['job_id'],
                None,
                digest(tenant),
                'a' * 64,
                digest(self.account('one')),
                digest(row),
                migration_archive_id='f' * 64 if change == 'archive' else digest(old),
            )
            before = deepcopy(self.db.rows)
            with self.assertRaises(self.fence.FenceRejected):
                self.transition.settle_resource(
                    self.db.transaction(),
                    self.db,
                    proof=proof,
                    expected_policy_sha256=digest(self.activation.read_policy(self.db)),
                    now=NOW,
                )
            self.assertEqual(self.db.rows, before)

    def saved_backlog_fixture(self):
        self.owner('one')
        job = self.job('one', 'approved-saved')
        cid = job['conversation_id']
        path = ('users', 'one', 'conversations', cid)
        source = deepcopy(self.db.rows[path])
        stock = str(uuid4())
        source.update(
            uid=None,
            capture_state='terminal',
            active_summary_version_id=stock,
            summary_versions=[{'id': stock, 'source': 'omi', 'kind': 'generated', 'is_active': True}],
        )
        job.update(status='blocked', dispatch_state='account_processing_held')
        self.db.put(path, source, created=job[self.lifecycle.TAG]['source_created_at'])
        self.db.put((self.activation.JOBS, job['job_id']), job)
        del self.db.rows[(self.lifecycle.ADMISSIONS, job[self.lifecycle.TAG]['marker_id'])]
        tenant = self.activation.tenant_ref(self.db, 'one').get().to_dict()
        tenant['quarantined_job_id'] = 'old-legacy-job'
        self.db.put(self.activation.tenant_ref(self.db, 'one').key, tenant)
        account = self.account('one')
        proof = self.lifecycle.VerifiedRoute(
            'one', account['route_sha256'], account['account_owner_sha256'], account['profile_sha256']
        )
        expected = {
            'eligible_metadata': True,
            'source_snapshot_sha256': digest(source),
            'job_snapshot_sha256': digest(job),
            'stock_version': stock,
            'job_id': job['job_id'],
            'first_completed_at': str(job['source_completed_at']),
            'source_created_at': str(job[self.lifecycle.TAG]['source_created_at']),
            'binding_sha256': digest(source['test_binding']),
            'client_interaction_id': job['client_interaction_id'],
            'transcript_sha256': job['transcript_sha256'],
        }
        spec = {
            'records': {cid: expected},
            'owner_sha256': hashlib.sha256(b'one').hexdigest(),
            'route': {k: account[k] for k in ('route_sha256', 'account_owner_sha256', 'profile_sha256')},
            'account_epoch': account['epoch'],
            'contract': 'synthetic-explicit-approved-backlog',
            'batch_id': 'synthetic-batch',
            'request_ref': 'synthetic-explicit-user-request',
        }
        tree = ast.parse((ROOT / 'candidate/database/explicit_saved_backlog_repair.py').read_text())
        selected = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in {'RepairRejected', 'need', 'plan', '_body'}
        ]
        scope = {
            'sha256': hashlib.sha256,
            'specification': lambda: spec,
            'AUDITS': 'synthetic_explicit_backlog_repairs',
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), '<actual-backlog-repair>', 'exec'), scope)
        build_identity = lambda **kwargs: SimpleNamespace(
            job_id=job['job_id'],
            client_interaction_id=job['client_interaction_id'],
            transcript_sha256=job['transcript_sha256'],
        )
        args = (
            self.db,
            'one',
            cid,
            proof,
            NOW,
            self.lifecycle,
            self.activation,
            sys.modules['database.normal_product_capture_provenance'],
            build_identity,
            lambda doc, uid: doc,
        )
        return scope, args, job, path, stock

    def test_actual_saved_backlog_preparation_accepts_v4_without_stopping_other_capture(self):
        scope, args, job, path, stock = self.saved_backlog_fixture()
        capture = ('users', 'one', 'capture_authority', 'current')
        active = {'conversation_id': 'different-current-source', 'state': 'active'}
        self.db.put(capture, active)
        before = deepcopy(self.db.rows[path])
        tx = self.db.transaction()
        result = scope['_body'](tx, *args)
        self.assertEqual(len(tx.writes), 4)
        tx.commit()
        self.assertEqual(result['status'], 'prepared_for_existing_worker')
        after = self.db.rows[path]
        self.assertEqual(after, {**before, 'uid': 'one'})
        self.assertEqual(after['active_summary_version_id'], stock)
        self.assertEqual(self.db.rows[capture], active)
        self.assertEqual(self.db.rows[(self.activation.JOBS, job['job_id'])]['attempt_count'], 0)
        tx = self.db.transaction()
        with self.assertRaises(scope['RepairRejected']):
            scope['_body'](tx, *args)
        self.assertEqual(tx.writes, [])

    def test_saved_backlog_still_rejects_selected_active_source_and_foreign_owner(self):
        for change in ('active_source', 'foreign_owner'):
            self.setUp()
            scope, args, job, path, stock = self.saved_backlog_fixture()
            args = list(args)
            if change == 'active_source':
                self.db.put(
                    ('users', 'one', 'capture_authority', 'current'),
                    {'conversation_id': job['conversation_id'], 'state': 'active'},
                )
            if change == 'foreign_owner':
                args[1] = 'foreign'
            tx = self.db.transaction()
            with self.subTest(change=change), self.assertRaises(scope['RepairRejected']):
                scope['_body'](tx, *args)
            self.assertEqual(tx.writes, [])

    def test_closed_invocation_cannot_use_generic_transaction_failure_fallback(self):
        self.owner('one')
        job = self.claim(self.job('one'))
        self.begin(job)
        scope, args, path, version = self.publication_fixture(job)
        self.finish(job)
        transaction_calls = []
        canonical_calls = []

        def fail_before_guard(*a, **k):
            transaction_calls.append(1)
            raise RuntimeError('synthetic datastore read failure')

        scope['conversations_db'].update_conversation_with_builder = fail_before_guard

        def canonical(*a, **k):
            canonical_calls.append(1)
            return {'ok': True}

        token = self.future._worker_job.set(job)
        try:
            with self.assertRaises(self.fence.FenceRejected):
                asyncio.run(scope['write_conversation_summary_cas'](**args, canonical_writer=canonical))
            self.assertEqual(transaction_calls, [])
            self.assertEqual(canonical_calls, [])
            self.assertEqual(self.db.rows[path]['summary_writeback_receipt']['status'], 'pending_reconciliation')
        finally:
            self.future._worker_job.reset(token)


if __name__ == '__main__':
    unittest.main()
