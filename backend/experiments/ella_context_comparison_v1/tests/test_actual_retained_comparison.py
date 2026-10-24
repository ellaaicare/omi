"""Actual complete retained service class, actual host hydrate, synthetic leaves.
No DB/network/provider or personal workspace. Imports/auth/storage are explicit
boundary doubles; no production binding classes are substituted or aliased.
"""

import ast, asyncio, copy, hashlib, importlib.util, io, json, sys, tempfile, threading, unittest
from contextlib import nullcontext, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / 'baseline/semantic_context_snapshot.py.source'
from importlib.machinery import SourceFileLoader

loader = SourceFileLoader('actual_context_host_for_comparison', str(HOST))
spec = importlib.util.spec_from_loader(loader.name, loader)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


def digest(v):
    return hashlib.sha256(json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def mod(name, **values):
    m = ModuleType(name)
    m.__dict__.update(values)
    return m


class Rejected(Exception):
    pass


class StopAtProduce(Exception):
    pass


async def await_stage(_, value):
    return await value


async def nohold(_):
    pass


class Rig:
    def __init__(self, mutation=None, diagnostic_throws=False):
        self.tmp = tempfile.TemporaryDirectory(prefix='ella-actual-context-comparison-', dir=ROOT)
        self.path = Path(self.tmp.name).resolve()
        self.path.chmod(0o700)
        self.runtime = SimpleNamespace(
            uid='synthetic-owner',
            provider='hermes',
            status='active',
            binding_id='binding',
            profile_name='profile',
            workspace_root=str(self.path),
            revision=1,
        )
        self.identity = SimpleNamespace(uid=self.runtime.uid, target_mode='hermes-cloud-transcript', digest='a' * 64)
        self.doc = {
            'id': 'synthetic-cid',
            'uid': self.runtime.uid,
            'status': 'completed',
            'active_summary_version_id': 'stock',
            'finished_at': 'fixed',
        }
        self.policy_commits = 0
        self.produce_calls = 0
        self.hydrations = 0
        self.mutation = mutation
        self.admission = SimpleNamespace(
            SemanticPublicationRejectedError=Rejected,
            digest=digest,
            POLICY_FIELD='policy',
            OPERATIONS_FIELD='ops',
            RECEIPTS_FIELD='receipts',
            current_binding=lambda *a, **k: None,
        )

        def canonical(**kw):
            return {'transcript': 'synthetic-source', 'finished_at': 'fixed'}

        scope = {
            '__name__': 'actual_retained_comparison_class',
            'asyncio': asyncio,
            'copy': copy,
            'threading': threading,
            'dataclass': dataclass,
            'SimpleNamespace': SimpleNamespace,
            'admission': self.admission,
            '_timing': SimpleNamespace(operation=lambda *a: nullcontext(), await_stage=await_stage),
            'canonical_source_from_conversation': canonical,
            'canonical_source_sha256': digest,
            'transcript_source': lambda d: ('synthetic-source', 'sourcehash'),
            'build_enrichment_identity': lambda **k: SimpleNamespace(
                client_interaction_id='client', transcript_sha256='sourcehash'
            ),
            'require_initial_source': lambda *a: None,
            'require_current_runtime': lambda *a: None,
            'require_transcript_processing_active': nohold,
            'JOBS_FIELD': 'semantic_retained_completion_jobs_v1',
            'MAX_JOBS': 8,
        }
        tree = ast.parse((ROOT / 'candidate/ella/services/retained_completion_enrichment.py.source').read_text())
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef))
            and n.name in {'reject', 'owned', 'source_pin', 'Dependencies', 'RetainedCompletionEnrichmentService'}
        ]
        sys.modules[scope['__name__']] = mod(scope['__name__'])
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                str(ROOT / 'candidate/ella/services/retained_completion_enrichment.py.source'),
                'exec',
            ),
            scope,
        )
        self.modules = {
            'database._client': mod('database._client', db=object()),
            'database.normal_account_lifecycle': mod(
                'database.normal_account_lifecycle', require_dispatch_context=lambda *a: None
            ),
            'ella.services.semantic_host_context': mod(
                'ella.services.semantic_host_context', HostReadBudget=lambda: object()
            ),
            'ella.services.summary_title_quality': mod(
                'ella.services.summary_title_quality', fresh_title_revision=lambda s: s
            ),
            'ella.services.semantic_companion_context': mod(
                'ella.services.semantic_companion_context',
                SOURCES=host.SOURCES,
                MAX_DOCUMENT_BYTES=host.MAX_DOCUMENT_BYTES,
                MAX_SNAPSHOT_BYTES=host.MAX_SNAPSHOT_BYTES,
            ),
        }
        ds = {}
        with patch.dict(sys.modules, self.modules):
            exec(
                compile(
                    (ROOT / 'candidate/ella/services/context_snapshot_diagnostics.py.source').read_text(),
                    'context_snapshot_diagnostics.py',
                    'exec',
                ),
                ds,
            )
        compare = ds['compare_context_snapshots']
        if diagnostic_throws:

            def compare(*a):
                raise RuntimeError('synthetic-private-do-not-export')

        self.modules['ella.services.context_snapshot_diagnostics'] = mod(
            'ella.services.context_snapshot_diagnostics', compare_context_snapshots=compare
        )

        async def resolve(*a, **k):
            return self.runtime

        async def user(**k):
            return {'confirmed': True, 'event_id': 'synthetic-event'}

        async def sync(*a, **k):
            return None

        async def learning(**k):
            return {'status': 'no_learning'}

        async def produce(**k):
            self.produce_calls += 1
            raise StopAtProduce()

        def hydrate(*a, **k):
            self.hydrations += 1
            if self.hydrations == 3 and self.mutation:
                self.mutation(self)
            return host.hydrate_owned_workspace(self.runtime, uid=self.runtime.uid, runtime_identity=self.identity)

        def update(uid, cid, builder):
            changes, result = builder(copy.deepcopy(self.doc))
            if 'policy' in changes:
                self.policy_commits += 1
            self.doc.update(copy.deepcopy(changes))
            return {'result': result}

        deps = scope['Dependencies'](
            selected=lambda uid: True,
            resolve=resolve,
            identity=lambda r: self.identity,
            revalidate=resolve,
            consent=lambda uid: uid,
            hydrate=hydrate,
            produce=produce,
            apply_factory=lambda **k: None,
            canonical_user=user,
            sync_context=sync,
            learning=learning,
        )
        self.service = scope['RetainedCompletionEnrichmentService'](
            repository=object(),
            read_conversation=lambda *a: copy.deepcopy(self.doc),
            update_conversation=update,
            dependencies=deps,
        )

    async def run(self):
        capture = io.StringIO()
        try:
            with patch.dict(sys.modules, self.modules), redirect_stdout(capture):
                await self.service._enrich(self.runtime.uid, 'synthetic-cid', False, 'client', 'sourcehash')
        finally:
            self.output = capture.getvalue()

    def close(self):
        self.tmp.cleanup()


class ActualRetainedTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_actual_hydrate_reaches_original_produce_boundary(self):
        r = Rig()
        try:
            with self.assertRaises(StopAtProduce):
                await r.run()
            self.assertEqual((r.policy_commits, r.produce_calls, r.hydrations), (1, 1, 3))
            self.assertEqual(r.output, '')
        finally:
            r.close()

    async def mismatch(self, mutation, throws=False):
        r = Rig(mutation, throws)
        try:
            with self.assertRaisesRegex(Rejected, '^retained_completion_context_changed$'):
                await r.run()
            self.assertEqual((r.policy_commits, r.produce_calls), (0, 0))
            self.assertNotIn('policy', r.doc)
            entry = next(iter(r.doc['semantic_retained_completion_jobs_v1'].values()))
            self.assertTrue(entry['canonical_user']['confirmed'])
            self.assertEqual(entry['learning']['status'], 'no_learning')
            self.assertEqual(entry['status'], 'started')
            self.assertNotIn('synthetic-private-do-not-export', r.output)
            return json.loads(r.output.removeprefix('ella_context_comparison ')) if r.output else None
        finally:
            r.close()

    async def test_missing_to_empty_raw_revision_preserves_rejection(self):
        d = await self.mismatch(lambda r: (r.path / 'MEMORY.md').write_text(''))
        self.assertTrue(d['normalized_documents_equal'])
        self.assertFalse(d['revision_equal'])
        self.assertEqual(d['changed_document_keys'], [])

    async def test_runtime_revision_preserves_rejection(self):
        d = await self.mismatch(lambda r: setattr(r.runtime, 'revision', 2))
        self.assertTrue(d['normalized_documents_equal'])
        self.assertFalse(d['revision_equal'])

    async def test_concurrent_synthetic_content_change_is_closed_metadata(self):
        d = await self.mismatch(lambda r: (r.path / 'MEMORY.md').write_text('synthetic-private-do-not-export'))
        self.assertFalse(d['normalized_documents_equal'])
        self.assertEqual(d['changed_document_keys'], ['MEMORY.md'])

    async def test_diagnostic_failure_cannot_replace_original_rejection(self):
        self.assertIsNone(await self.mismatch(lambda r: (r.path / 'MEMORY.md').write_text('changed'), True))


if __name__ == '__main__':
    unittest.main()
