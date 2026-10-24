"""Actual coordinator/ledger/bridge composition, synthetic transport/storage.

Pure semantic request/review leaves and external authority/event/DB adapters are
explicit doubles. No network, credential, real account or provider is accessed.
"""

import ast
import asyncio
import copy
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SERVING = ROOT.parents[1] / 'serving_snapshots/unified-canonical-memory-4235154f/backend'
spec = importlib.util.spec_from_file_location(
    'context_fence_actual_budget', ROOT / 'baseline/ella/services/semantic_operation_budget.py'
)
budget = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = budget
spec.loader.exec_module(budget)
JOB = {
    'job_id': 'synthetic-job',
    'uid': 'synthetic-owner',
    'conversation_id': 'synthetic-cid',
    'attempt_count': 1,
    'lease_token': 'synthetic-lease',
}


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def reject(code):
    raise ValueError(code)


def mod(name, **kw):
    x = ModuleType(name)
    x.__dict__.update(kw)
    return x


async def await_stage(_, value):
    return await value


TIMING = SimpleNamespace(stage=lambda _: nullcontext(), await_stage=await_stage)


def definitions(path, names, scope):
    path = path if path.exists() else path.with_suffix(path.suffix + ".source")
    tree = ast.parse(path.read_text())
    nodes = [
        n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names
    ]
    code = ast.Module(
        body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)] + nodes,
        type_ignores=[],
    )
    name = scope.setdefault('__name__', 'context_synthetic_scope')
    sys.modules[name] = mod(name, **{k: v for k, v in scope.items() if k != '__name__'})
    exec(compile(ast.fix_missing_locations(code), str(path), 'exec'), scope)
    sys.modules[name].__dict__.update(scope)


class Rig:
    def __init__(self, *, source='candidate', mutation=None, delay=0, legacy=False, hook=True):
        self.clock = 0.0
        self.delay = delay
        self.mutation = mutation
        self.legacy = legacy
        self.hook = hook
        self.counts = {
            'context': 0,
            'provider': 0,
            'canonical_write': 0,
            'canonical_read': 0,
            'source_hook': 0,
            'source_read': 0,
            'publication': 0,
        }
        self.events = []
        self.stored = {}
        self.owner = JOB['uid']
        self.identity = 'original-identity'
        self.consent = True
        self.graph = True
        self.context = {
            'owner_uid': JOB['uid'],
            'revision': 'fictional-r1',
            'documents': {'MEMORY.md': 'Fictional companion data.'},
        }
        bind_scope = {'dataclass': dataclass, 'asdict': asdict, '_reject': reject}
        definitions(SERVING / 'ella/services/semantic_publication.py', {'PublicationBinding'}, bind_scope)
        self.Binding = bind_scope['PublicationBinding']
        self.bound = self.Binding(
            JOB['uid'],
            JOB['conversation_id'],
            'a' * 64,
            'source-v1',
            'stock-v1',
            'policy-v1',
            digest(self.context),
            'fictional-r1',
            'b' * 64,
        )
        self.doc = {
            'id': JOB['conversation_id'],
            'uid': JOB['uid'],
            'binding': self.bound,
            'schema': 2,
            'contract': 2,
            'transcript_segments': [{'text': 'Fictional source only.'}],
        }
        self.admission = SimpleNamespace(
            PublicationBinding=self.Binding,
            SemanticPublicationRejectedError=ValueError,
            digest=digest,
            semantic_contract_version=lambda d: d.get('contract'),
            candidate_schema=lambda d: d.get('schema'),
            current_binding=lambda d, **kw: d['binding'],
            record_completed_operation=lambda *a, **kw: ({}, None),
            issue_receipt=self.issue_receipt,
        )
        self.ns = {
            '__name__': 'actual_context_composition',
            'asyncio': asyncio,
            'copy': copy,
            'json': json,
            'threading': threading,
            'time': time,
            'asdict': asdict,
            'dataclass': dataclass,
            'Callable': object,
            '_timing': TIMING,
            'admission': self.admission,
            'current_budget': budget.current_budget,
            'remaining_timeout': budget.remaining_timeout,
            'EXECUTIONS_FIELD': 'semantic_producer_executions_v1',
            'MAX_OPERATIONS': 8,
            'MAX_RESULT_BYTES': 16000,
            'canonical_source_from_conversation': lambda **kw: {
                'uid': kw['uid'],
                'conversation_id': kw['conversation_id'],
            },
        }
        definitions(
            ROOT / source / 'ella/services/semantic_producer_ledger.py',
            {'BackendTurn', 'TrustedRuntimeAdapter', 'DurableServerLedger', 'run_versioned_producer'},
            self.ns,
        )
        self.producer_ns = {
            'deepcopy': copy.deepcopy,
            'POLICY': 'ella-semantic-summary-v1',
            'ContractError': ValueError,
            'generate_requests': self.requests,
            'validate_stored_review': lambda **kw: self.summary(),
            'digest': digest,
        }
        definitions(SERVING / 'ella/services/semantic_contextual_producer_v3.py', {'produce'}, self.producer_ns)
        self.producer_ns['PublicationBinding'] = self.Binding
        self.modules = {
            'ella': mod('ella'),
            'ella.services': mod('ella.services'),
            'ella.services.semantic_producer_v1': mod(
                'ella.services.semantic_producer_v1', PublicationBinding=self.Binding
            ),
            'ella.services.semantic_contextual_producer_v3': mod(
                'ella.services.semantic_contextual_producer_v3',
                produce=self.producer_ns['produce'],
                generate_requests=self.requests,
                validate_stored_review=lambda **kw: self.summary(),
            ),
            'ella.services.transcript_processing_hold': mod(
                'ella.services.transcript_processing_hold', require_transcript_processing_active=self.hold
            ),
        }
        self.bridge_ns = {
            **self.ns,
            'PublicationBinding': self.Binding,
            'Any': object,
            'datetime': datetime,
            'timezone': timezone,
            'digest': digest,
            '_reject': reject,
            'SELF_HOSTED_CHANNELS': {'generator': 'generator', 'verifier': 'verifier'},
            'ROLE_POLICIES': {'generator': 'ella-semantic-summary-v1', 'verifier': 'ella-semantic-summary-v1'},
            'MAX_REQUEST_BYTES': 196608,
            'require_queue_graph': self.require_graph,
            '_valid_response_id': lambda v: bool(v),
            '_parse_result': json.loads,
            '_require_semantic_protocol_ack': lambda *a, **k: {'provider_response_id': 'synthetic-provider-id'},
        }
        definitions(
            ROOT / source / 'ella/services/semantic_self_hosted_runtime.py',
            {'SelfHostedSemanticRuntimeBridge'},
            self.bridge_ns,
        )
        self.bridge = object.__new__(self.bridge_ns['SelfHostedSemanticRuntimeBridge'])
        self.bridge.binding = self.bound
        self.bridge.expected_runtime_identity = self.identity
        self.bridge.repository = None
        self.bridge.timeout_seconds = 105
        self.bridge.current_binding = self.current_full
        runtime = SimpleNamespace(
            uid=JOB['uid'],
            provider='hermes',
            status='active',
            gateway_url='https://synthetic.invalid',
            gateway_token='synthetic-not-a-credential',
            agent_id='synthetic-agent',
        )

        async def resolve(*a, **k):
            return runtime

        async def consent(uid):
            return uid if self.consent else 'revoked'

        # Actual _authority executes consent in to_thread, so its leaf is sync.
        self.bridge._deps = SimpleNamespace(
            resolver=resolve,
            revalidator=resolve,
            consent=lambda uid: uid if self.consent else 'revoked',
            identity=lambda r: self.identity,
            client_factory=self.client_factory,
            event_type=self.event_type,
        )
        self.bridge.event_store = SimpleNamespace(write_batch=self.write_batch, get_event=self.get_event)

    def summary(self):
        return {'title': 'Fictional', 'overview': 'Fictional result', 'emoji': 'x', 'category': 'other'}

    def issue_receipt(self, *a, **kw):
        self.counts['publication'] += 1
        return {}, {'receipt_id': 'synthetic-receipt'}

    def mutate(self, stage):
        if self.mutation == stage:
            self.context['revision'] = 'changed'
        if self.mutation == stage + '_owner':
            self.owner = 'foreign'
        if self.mutation == stage + '_consent':
            self.consent = False
        if self.mutation == stage + '_runtime':
            self.identity = 'foreign-identity'
        if self.mutation == stage + '_source':
            self.doc['binding'] = replace(self.bound, source_sha256='c' * 64)
        if self.mutation == stage + '_expiry':
            self.clock = 600

    def read(self, uid, cid):
        self.counts['source_read'] += 1
        assert (uid, cid) == (JOB['uid'], JOB['conversation_id'])
        return copy.deepcopy({**self.doc, 'uid': self.owner})

    def update(self, uid, cid, builder):
        d = self.read(uid, cid)
        update, result = builder(d)
        self.doc.update(update)
        return {'result': result}

    def source(self):
        self.counts['source_hook'] += 1
        if self.mutation == 'hook_expiry':
            self.clock = 600
        if self.mutation == 'hook_owner':
            return replace(self.bound, uid='foreign')
        if self.mutation == 'hook_policy':
            self.doc['schema'] = 1
        if self.mutation == 'hook_changed_source':
            self.doc['binding'] = replace(self.bound, source_sha256='c' * 64)
        return self.bound

    def context_read(self, uid):
        assert uid == JOB['uid']
        self.counts['context'] += 1
        self.clock += self.delay
        if self.clock > 480:
            raise TimeoutError('synthetic_host_budget_exhausted')
        return copy.deepcopy(self.context)

    def current_full(self):
        snapshot = self.context_read(JOB['uid'])
        d = self.read(JOB['uid'], JOB['conversation_id'])
        if (
            d['uid'] != JOB['uid']
            or d['binding'] != self.bound
            or digest(snapshot) != self.bound.companion_context_sha256
        ):
            reject('context_or_source_changed')
        return self.bound

    async def hold(self, uid):
        if not self.consent:
            reject('consent_revoked')

    def require_graph(self, *args):
        if not self.graph:
            reject('closed_graph')

    def requests(self, *, candidate=None, companion_context, **kw):
        if digest(companion_context) != self.bound.companion_context_sha256:
            reject('context_changed_request')
        common = {
            'policy': 'ella-semantic-summary-v1',
            'instructions': 'Fictional instruction',
            'semantic_contract_version': 2,
            'schema_version': 2,
            'context': copy.deepcopy(companion_context),
        }
        return {**common, 'role': 'generator'}, (
            {**common, 'role': 'verifier', 'candidate': candidate} if candidate is not None else None
        )

    def client_factory(self, **kwargs):
        rig = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *a, **kw):
                rig.counts['provider'] += 1
                role = kw['headers']['X-Ella-Semantic-Role']
                rig.events.append('provider:' + role)
                rig.mutate('during_' + role)
                rig.clock += 10

                class Response:
                    headers = {}

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return {
                            'id': 'synthetic-gateway-id',
                            'choices': [
                                {
                                    'finish_reason': 'stop',
                                    'message': {
                                        'role': 'assistant',
                                        'content': json.dumps({'outcome': 'candidate'}),
                                        'tool_calls': None,
                                    },
                                }
                            ],
                        }

                return Response()

        return Client()

    def event_type(self, **kw):
        self.mutate('before_canonical')
        return SimpleNamespace(**kw)

    async def write_batch(self, events):
        self.counts['canonical_write'] += 1
        e = events[0]
        self.stored = e.__dict__
        self.events.append('canonical:' + e.channel)
        return {'ok': True, 'events': [{'event_id': e.event_id, 'source_identity': e.source_ref['source_identity']}]}

    async def get_event(self, **kw):
        self.counts['canonical_read'] += 1
        self.mutate('during_canonical_readback')
        return self.stored

    async def run_async(self):
        return await self.ns['run_versioned_producer'](
            uid=JOB['uid'],
            conversation_id=JOB['conversation_id'],
            read_conversation=self.read,
            update_conversation=self.update,
            read_companion_context=self.context_read,
            run_backend_turn=self.bridge,
            provider_deadline_seconds=90,
            role_deadline_seconds=120,
            candidate_schema_version=2,
            semantic_contract_version=2,
            **({'read_source_binding': self.source} if self.hook else {})
        )

    def run(self):
        with patch.dict(sys.modules, self.modules):
            if self.legacy:
                return asyncio.run(self.run_async())
            b = budget.OperationBudget(budget.attempt_scope(JOB), 600, clock=lambda: self.clock)
            token = budget._CURRENT.set(b)
            try:
                return asyncio.run(self.run_async())
            finally:
                budget._CURRENT.reset(token)


class Tests(unittest.TestCase):
    def test_actual_composition_consolidates_21_full_reads_to_8_without_skipping_models_or_publication(self):
        old = Rig(source='baseline', hook=False)
        new = Rig()
        self.assertEqual(old.run()['summary'], new.run()['summary'])
        self.assertEqual((old.counts['context'], new.counts['context']), (21, 8))
        for rig in (old, new):
            self.assertEqual(
                (rig.counts['provider'], rig.counts['canonical_write'], rig.counts['publication']), (2, 2, 1)
            )

    def test_synthetic_slow_reads_fit_existing_budget_without_increasing_any_limit(self):
        old = Rig(source='baseline', hook=False, delay=25)
        with self.assertRaises(TimeoutError):
            old.run()
        new = Rig(delay=25)
        self.assertEqual(new.run()['publication_receipt_id'], 'synthetic-receipt')
        self.assertLess(new.clock, 480)

    def test_before_dispatch_context_and_source_owner_runtime_consent_drift_rejects_without_provider(self):
        for change in ['context', 'owner', 'source', 'runtime', 'consent']:
            with self.subTest(change=change):
                r = Rig()
                if change == 'context':
                    r.context['revision'] = 'changed'
                elif change == 'owner':
                    r.owner = 'foreign'
                elif change == 'source':
                    r.doc['binding'] = replace(r.bound, source_sha256='c' * 64)
                elif change == 'runtime':
                    r.identity = 'foreign'
                else:
                    r.consent = False
                with self.assertRaises(ValueError):
                    r.run()
                self.assertEqual(r.counts['provider'], 0)
                self.assertEqual(r.counts['canonical_write'], 0)

    def test_mutation_during_model_rejects_before_canonical_or_next_role(self):
        for change in [
            'during_generator',
            'during_generator_owner',
            'during_generator_source',
            'during_generator_runtime',
            'during_generator_consent',
        ]:
            with self.subTest(change=change):
                r = Rig(mutation=change)
                with self.assertRaises(ValueError):
                    r.run()
                self.assertEqual(r.counts['provider'], 1)
                self.assertEqual(r.counts['canonical_write'], 0)
                self.assertEqual(r.counts['publication'], 0)

    def test_before_canonical_and_readback_context_drift_never_publishes_or_starts_verifier(self):
        for stage, writes in [('before_canonical', 0), ('during_canonical_readback', 1)]:
            with self.subTest(stage=stage):
                r = Rig(mutation=stage)
                with self.assertRaises(ValueError):
                    r.run()
                self.assertEqual(r.counts['provider'], 1)
                self.assertEqual(r.counts['canonical_write'], writes)
                self.assertEqual(r.counts['publication'], 0)

    def test_verifier_drift_prevents_publication(self):
        r = Rig(mutation='during_verifier')
        with self.assertRaises(ValueError):
            r.run()
        self.assertEqual(r.counts['provider'], 2)
        self.assertEqual(r.counts['publication'], 0)

    def test_source_hook_return_resample_deadline_and_schema_reject_without_egress(self):
        for mutation, error in [
            ('hook_owner', ValueError),
            ('hook_changed_source', ValueError),
            ('hook_policy', ValueError),
            ('hook_expiry', TimeoutError),
        ]:
            with self.subTest(mutation=mutation):
                r = Rig(mutation=mutation)
                with self.assertRaises(error):
                    r.run()
                self.assertEqual(r.counts['provider'], 0)

    def test_provider_or_canonical_deadline_expiry_prevents_later_effect(self):
        for stage in ['during_generator_expiry', 'before_canonical_expiry']:
            with self.subTest(stage=stage):
                r = Rig(mutation=stage)
                with self.assertRaises(TimeoutError):
                    r.run()
                self.assertEqual(r.counts['canonical_write'], 0)

    def test_default_none_and_legacy_do_not_enable_source_only_hook(self):
        r = Rig(hook=False)
        r.run()
        self.assertEqual(r.counts['context'], 17)
        r = Rig(legacy=True, hook=False)
        r.run()
        self.assertEqual(r.counts['context'], 21)
        with self.assertRaises(ValueError):
            Rig(legacy=True, hook=True).run()

    def test_final_publication_rehydrates_exact_context_and_detects_mutation(self):
        r = Rig()
        original = r.context_read

        def read(uid):
            # Initial data plus three critical fences for each completed role.
            # The eighth context read is the actual ledger publication CAS.
            if r.counts['context'] == 7:
                r.context['revision'] = 'changed-before-publication'
            return original(uid)

        r.context_read = read
        with self.assertRaises(ValueError):
            r.run()
        self.assertEqual(r.counts['provider'], 2)
        self.assertEqual(r.counts['publication'], 0)

    def test_cancellation_after_source_hook_rejects_without_egress(self):
        r = Rig()
        cancelled = threading.Event()
        original = r.source
        r.ns['threading'] = SimpleNamespace(Event=lambda: cancelled)

        def source():
            cancelled.set()
            return original()

        r.source = source
        with self.assertRaises(ValueError):
            r.run()
        self.assertEqual(r.counts['provider'], 0)

    def test_attempt_or_lease_context_change_inside_hook_rejects_without_egress(self):
        r = Rig()
        original = r.source

        def source():
            changed = {**JOB, 'lease_token': 'different-attempt-lease'}
            budget._CURRENT.set(budget.OperationBudget(budget.attempt_scope(changed), 600, clock=lambda: r.clock))
            return original()

        r.source = source
        with self.assertRaises(ValueError):
            r.run()
        self.assertEqual(r.counts['provider'], 0)

    def test_closed_attempt_fence_still_prevents_model(self):
        r = Rig()
        r.graph = False
        with self.assertRaises(ValueError):
            r.run()
        self.assertEqual(r.counts['provider'], 0)

    def test_full_authority_method_and_final_context_publication_logic_are_unchanged(self):
        def node(kind, name, member=None):
            path = ROOT / kind / 'ella/services' / name
            path = path if path.exists() else path.with_suffix(path.suffix + '.source')
            tree = ast.parse(path.read_text())
            cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == member[0])
            return next(
                n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == member[1]
            )

        for filename, member in [
            ('semantic_self_hosted_runtime.py', ('SelfHostedSemanticRuntimeBridge', '_authority')),
            ('semantic_producer_ledger.py', ('DurableServerLedger', 'issue_publication')),
        ]:
            self.assertEqual(
                ast.dump(node('baseline', filename, member)), ast.dump(node('candidate', filename, member))
            )


if __name__ == '__main__':
    unittest.main(verbosity=2)
