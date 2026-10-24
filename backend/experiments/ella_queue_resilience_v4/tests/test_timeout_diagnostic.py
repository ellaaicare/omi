from test_shared_operation_budget import budget, JOB

"""Execute the actual dispatcher function with synthetic dependencies only."""
import ast
import asyncio
import dataclasses
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'timeout_diagnostic', ROOT / 'candidate/ella/services/causal_diagnostics.py'
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class ProvisioningError(Exception):
    pass


@dataclasses.dataclass
class DeliveryResult:
    ok: bool
    error_code: str = ''
    retryable: bool = True
    receipt: object = None


class TimeoutDiagnosticTests(unittest.TestCase):
    def invoke(self, error):
        tree = ast.parse((ROOT / 'candidate/ella/services/hermes_cloud_enrichment_outbox.py').read_text())
        function = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == 'direct_enrichment')
        function.decorator_list = []
        calls = []

        def emit(exc, stage):
            calls.append(type(exc).__name__)
            return diag.safe_exception(exc, stage)

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

        class Service:
            count = 0

            async def enrich(self, **kwargs):
                self.count += 1
                raise error

        service = Service()
        runtime = types.ModuleType('ella.services.runtime_errors')
        runtime.ProvisioningError = ProvisioningError
        with patch.dict(sys.modules, {'ella.services.runtime_errors': runtime}):
            result = asyncio.run(
                scope['direct_enrichment'](
                    service,
                    {
                        **JOB,
                        'uid': 'synthetic',
                        'conversation_id': 'synthetic',
                        'client_interaction_id': 'synthetic',
                        'transcript_sha256': 'a' * 64,
                    },
                )
            )
        return result, calls, service.count

    def test_timeout_records_closed_metadata_and_never_retries(self):
        error = TimeoutError('private content must not be serialized')
        result, calls, count = self.invoke(error)
        self.assertEqual(count, 1)
        self.assertEqual(calls, ['TimeoutError'])
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertEqual(result.error_code, 'normal_enrichment_timeout_unknown')
        value = result.receipt['failure_diagnostic']
        self.assertEqual(value['exception_type'], 'TimeoutError')
        self.assertEqual(diag.sanitize_diagnostic(value), value)
        self.assertNotIn('private content', repr(value))

    def test_timeout_keeps_bounded_cause_metadata(self):
        try:
            try:
                raise ConnectionError('private endpoint')
            except ConnectionError as cause:
                raise TimeoutError('private timeout') from cause
        except TimeoutError as error:
            result, _, _ = self.invoke(error)
        self.assertEqual(result.receipt['failure_diagnostic']['cause_types'], ['TimeoutError', 'OtherException'])
        self.assertNotIn('private', repr(result.receipt))

    def test_cancellation_remains_unresolved_and_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            self.invoke(asyncio.CancelledError())


if __name__ == '__main__':
    unittest.main()
