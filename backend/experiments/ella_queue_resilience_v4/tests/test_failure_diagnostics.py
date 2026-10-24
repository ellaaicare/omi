from test_shared_operation_budget import budget, JOB
import ast, asyncio, copy, dataclasses, importlib.util, json, sys, types, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'closed_diagnostics_test', ROOT / 'candidate/ella/services/causal_diagnostics.py'
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class SemanticPublicationRejectedError(ValueError):
    pass


class ProvisioningError(ValueError):
    def __init__(self, code):
        self.code = code


class DiagnosticTests(unittest.TestCase):
    def error(self, reason):
        try:
            try:
                raise SemanticPublicationRejectedError(reason)
            except SemanticPublicationRejectedError as exc:
                raise ProvisioningError('retained_completion_rejected') from exc
        except ProvisioningError as exc:
            return exc

    def test_closed_inner_reason_survives_mapping(self):
        value = diag.safe_exception(
            self.error('retained_completion_authority_unavailable'), 'normal_enrichment_dispatch'
        )
        self.assertEqual(value['cause_metadata'][1]['closed_code'], 'retained_completion_authority_unavailable')
        self.assertEqual(diag.sanitize_diagnostic(value), value)

    def test_arbitrary_private_reason_and_extra_payload_rejected(self):
        secret = 'private transcript and bearer credential'
        value = diag.safe_exception(self.error(secret), 'normal_enrichment_dispatch')
        self.assertNotIn(secret, json.dumps(value))
        self.assertNotIn('closed_code', value['cause_metadata'][1])
        for change in (
            {'raw_transcript': secret},
            {'cause_metadata': [{'exception_type': 'ContractError', 'closed_code': secret, 'frames': []}]},
            {'frames': [{'file': 'private.py', 'function': 'secret', 'line': 1}]},
            {'http_status': True},
            {'cause_types': ['x'] * 5},
        ):
            with self.subTest(change=tuple(change)):
                self.assertIsNone(diag.sanitize_diagnostic({**value, **change}))

    def test_hostile_exception_metadata_fails_without_private_serialization(self):
        class Bad:
            def __str__(self):
                raise AssertionError('must not stringify private value')

        value = diag.safe_exception(self.error(Bad()), 'normal_enrichment_dispatch')
        self.assertIsNotNone(diag.sanitize_diagnostic(value))
        self.assertNotIn('closed_code', value['cause_metadata'][1])

    def test_known_branch_actual_function_captures_cause_without_provider(self):
        tree = ast.parse((ROOT / 'candidate/ella/services/hermes_cloud_enrichment_outbox.py').read_text())
        function = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == 'direct_enrichment')
        function.decorator_list = []

        @dataclasses.dataclass
        class DeliveryResult:
            ok: bool
            error_code: str = ''
            retryable: bool = True
            receipt: object = None

        captured = []

        def emit(e, stage):
            value = diag.safe_exception(e, stage)
            captured.append(value)
            return value

        scope = {
            'asyncio': asyncio,
            'DeliveryResult': DeliveryResult,
            'emit_causal_diagnostic': emit,
            'operation_budget_scope': budget.operation_budget_scope,
        }
        exec(
            compile(
                ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                'actual_direct_enrichment',
                'exec',
            ),
            scope,
        )
        error = self.error('retained_completion_context_changed')

        class Service:
            async def enrich(self, **kwargs):
                raise error

        runtime = types.ModuleType('ella.services.runtime_errors')
        runtime.ProvisioningError = ProvisioningError
        with patch.dict(sys.modules, {'ella.services.runtime_errors': runtime}):
            result = asyncio.run(
                scope['direct_enrichment'](
                    Service(),
                    {
                        **JOB,
                        'uid': 'synthetic-owner',
                        'conversation_id': 'synthetic-source',
                        'client_interaction_id': 'synthetic-client',
                        'transcript_sha256': 'a' * 64,
                    },
                )
            )
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(result.error_code, 'retained_completion_rejected')
        self.assertEqual(
            result.receipt['failure_diagnostic']['cause_metadata'][1]['closed_code'],
            'retained_completion_context_changed',
        )
        self.assertEqual(len(captured), 1)


if __name__ == '__main__':
    unittest.main()
