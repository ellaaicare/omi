"""No network: actual adapter and budget with controlled clocks/thread loop."""

import ast
import asyncio
import copy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'shared_budget_actual', ROOT / 'candidate/ella/services/semantic_operation_budget.py'
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


@dataclass
class BackendTurn:
    result: dict
    runtime_interaction_id: str
    canonical_assistant_event_id: str


class SharedBudgetTests(unittest.TestCase):
    def adapter_class(self):
        tree = ast.parse((ROOT / 'candidate/ella/services/semantic_producer_ledger.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'TrustedRuntimeAdapter')

        @contextmanager
        def stage(*args):
            yield

        async def await_stage(label, value):
            return await value

        scope = {
            'asyncio': asyncio,
            'time': __import__('time'),
            'copy': copy,
            'threading': threading,
            'BackendTurn': BackendTurn,
            'current_budget': budget.current_budget,
            '_timing': types.SimpleNamespace(await_stage=await_stage),
            'admission': types.SimpleNamespace(
                SemanticPublicationRejectedError=ValueError, digest=lambda _: 'synthetic-hash'
            ),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_adapter', 'exec'), scope)
        return scope['TrustedRuntimeAdapter']

    def test_same_expiry_nested_scope_and_full_attempt_binding(self):
        with budget.operation_budget_scope(JOB) as first:
            with budget.operation_budget_scope(JOB) as nested:
                self.assertIs(first, nested)
                self.assertEqual(first.deadline, nested.deadline)
            for change in (
                {'uid': 'foreign'},
                {'conversation_id': 'foreign'},
                {'attempt_count': 2},
                {'lease_token': 'stale'},
                {'job_id': 'foreign'},
            ):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    with budget.operation_budget_scope({**JOB, **change}):
                        pass
        self.assertIsNone(budget.current_budget())

    def test_expired_budget_rejects_new_dispatch_without_coroutine(self):
        clock = [0.0]
        value = budget.OperationBudget(budget.attempt_scope(JOB), 600, clock=lambda: clock[0])
        token = budget._CURRENT.set(value)
        try:
            adapter = self.adapter_class()(loop=None, run_turn=None, role='generator', observations={}, deadline=120)
            clock[0] = 600
            with patch.object(asyncio, 'run_coroutine_threadsafe', side_effect=AssertionError('egress')):
                with self.assertRaises(TimeoutError):
                    adapter({})
        finally:
            budget._CURRENT.reset(token)

    def test_generator_and_verifier_share_remainder_and_preserve_shorter_cap(self):
        clock = [10.0]
        value = budget.OperationBudget(budget.attempt_scope(JOB), 600, clock=lambda: clock[0])
        token = budget._CURRENT.set(value)
        waits = []

        class Future:
            def result(self, timeout):
                waits.append(timeout)
                clock[0] += min(40, max(0, timeout - 1))
                return BackendTurn({}, 'synthetic-runtime', 'synthetic-event')

            def cancel(self):
                pass

        def submit(coroutine, loop):
            coroutine.close()
            return Future()

        try:
            with patch.object(asyncio, 'run_coroutine_threadsafe', side_effect=submit):
                for role, cap in [('generator', 120), ('verifier', 120), ('generator', 30)]:
                    self.adapter_class()(loop=None, run_turn=None, role=role, observations={}, deadline=cap)({})
                clock[0] = 605
                self.adapter_class()(loop=None, run_turn=None, role='generator', observations={}, deadline=30)({})
            self.assertEqual(waits, [600, 560, 30, 5])
            self.assertEqual(value.deadline, 610)
        finally:
            budget._CURRENT.reset(token)

    def test_manual_caller_keeps_120_and_stale_attempt_context_is_rejected(self):
        Adapter = self.adapter_class()
        adapter = Adapter(loop=None, run_turn=None, role='generator', observations={}, deadline=120)
        self.assertIsNone(adapter.operation_budget)
        with budget.operation_budget_scope(JOB):
            owned = Adapter(loop=None, run_turn=None, role='generator', observations={}, deadline=120)
        with budget.operation_budget_scope({**JOB, 'lease_token': 'other'}):
            with self.assertRaises(ValueError):
                owned({})

    def test_actual_thread_to_loop_propagates_same_budget(self):
        async def scenario():
            with budget.operation_budget_scope(JOB) as original:

                async def turn(**kwargs):
                    self.assertIs(budget.current_budget(), original)
                    return BackendTurn({}, 'synthetic-runtime', 'synthetic-event')

                adapter = self.adapter_class()(
                    loop=asyncio.get_running_loop(), run_turn=turn, role='generator', observations={}, deadline=120
                )
                result = await asyncio.to_thread(adapter, {})
                self.assertEqual(result, {})

        asyncio.run(scenario())

    def test_late_return_or_cancellation_cannot_record_observations(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                clock = [0.0]
                value = budget.OperationBudget(budget.attempt_scope(JOB), 600, clock=lambda: clock[0])
                token = budget._CURRENT.set(value)
                cancellation = threading.Event()
                observations = {}
                cancel = []

                class Future:
                    def result(self, timeout):
                        if cancelled:
                            cancellation.set()
                        else:
                            clock[0] = 601
                        return BackendTurn({}, 'synthetic-runtime', 'synthetic-event')

                    def cancel(self):
                        cancel.append(True)

                def submit(coroutine, loop):
                    coroutine.close()
                    return Future()

                try:
                    with patch.object(asyncio, 'run_coroutine_threadsafe', side_effect=submit):
                        adapter = self.adapter_class()(
                            loop=None,
                            run_turn=None,
                            role='generator',
                            observations=observations,
                            deadline=120,
                            cancellation=cancellation,
                        )
                        with self.assertRaises((TimeoutError, ValueError)):
                            adapter({})
                    self.assertEqual(observations, {})
                    self.assertEqual(cancel, [True])
                finally:
                    budget._CURRENT.reset(token)

    def test_timeout_cancels_future_and_does_not_record_completion(self):
        calls = []

        class Future:
            def result(self, timeout):
                raise TimeoutError()

            def cancel(self):
                calls.append('cancel')

        def submit(coroutine, loop):
            coroutine.close()
            return Future()

        observations = {}
        with budget.operation_budget_scope(JOB):
            adapter = self.adapter_class()(
                loop=None, run_turn=None, role='generator', observations=observations, deadline=120
            )
            with patch.object(asyncio, 'run_coroutine_threadsafe', side_effect=submit):
                with self.assertRaises(TimeoutError):
                    adapter({})
        self.assertEqual(calls, ['cancel'])
        self.assertEqual(observations, {})

    def test_actual_host_read_budget_clamps_without_renewing_original_limits(self):
        tree = ast.parse((ROOT / 'candidate/ella/services/semantic_host_context.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HostReadBudget')
        scope = {
            'time': __import__('time'),
            'threading': threading,
            'CompanionContextUnavailable': ValueError,
            'remaining_timeout': budget.remaining_timeout,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_host_budget', 'exec'), scope)
        clock = [0.0]
        value = budget.OperationBudget(budget.attempt_scope(JOB), 10, clock=lambda: clock[0])
        token = budget._CURRENT.set(value)
        try:
            reads = scope['HostReadBudget'](wall_seconds=480, max_calls=3, clock=lambda: clock[0])
            self.assertEqual(reads.reserve('context_snapshot'), 10)
            clock[0] = 8
            self.assertEqual(reads.reserve('context_snapshot'), 2)
            self.assertEqual(reads.deadline, 480)
            self.assertEqual(reads.calls, 2)
            clock[0] = 10
            with self.assertRaises(TimeoutError):
                reads.reserve('context_snapshot')
        finally:
            budget._CURRENT.reset(token)

    def test_actual_bridge_cannot_send_if_budget_expires_in_last_authority(self):
        from contextlib import nullcontext
        from dataclasses import asdict
        from datetime import datetime, timezone
        import json

        tree = ast.parse((ROOT / 'candidate/ella/services/semantic_self_hosted_runtime.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SelfHostedSemanticRuntimeBridge')

        async def stage(label, value):
            return await value

        scope = {
            '_timing': types.SimpleNamespace(stage=lambda _: nullcontext(), await_stage=stage),
            'current_budget': budget.current_budget,
            'remaining_timeout': budget.remaining_timeout,
            'SELF_HOSTED_CHANNELS': {'generator': 'generator'},
            'ROLE_POLICIES': {'generator': 'synthetic'},
            'copy': copy,
            'json': json,
            'MAX_REQUEST_BYTES': 100000,
            'digest': lambda _: 'a' * 64,
            'asdict': asdict,
            'datetime': datetime,
            'timezone': timezone,
            'asyncio': asyncio,
            'require_queue_graph': lambda *_: None,
            '_reject': lambda code: (_ for _ in ()).throw(ValueError(code)),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_bridge', 'exec'), scope)
        bridge = object.__new__(scope['SelfHostedSemanticRuntimeBridge'])

        @dataclass
        class Binding:
            uid: str
            conversation_id: str

        bridge.binding = Binding(JOB['uid'], JOB['conversation_id'])
        bridge.timeout_seconds = 105
        clock = [0.0]
        value = budget.OperationBudget(budget.attempt_scope(JOB), 600, clock=lambda: clock[0])
        token = budget._CURRENT.set(value)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def post(self, *args, **kwargs):
                raise AssertionError('expired provider send')

        bridge._deps = types.SimpleNamespace(client_factory=lambda **kwargs: Client())

        async def authority(identity=None):
            if identity is not None:
                clock[0] = 600
            return (
                types.SimpleNamespace(gateway_url='https://synthetic.invalid', gateway_token='synthetic'),
                'synthetic-identity',
            )

        bridge._authority = authority
        try:
            with self.assertRaises(TimeoutError):
                asyncio.run(bridge(role='generator', request={'policy': 'synthetic', 'instructions': 'synthetic'}))
        finally:
            budget._CURRENT.reset(token)

    def test_invalid_type_expiry_and_context_reset(self):
        token = budget._CURRENT.set({'deadline': 999999})
        try:
            with self.assertRaises(ValueError):
                budget.current_budget()
        finally:
            budget._CURRENT.reset(token)
        for seconds in (True, 0, -1, 601, float('inf'), float('nan')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                budget.OperationBudget(budget.attempt_scope(JOB), seconds)
        with self.assertRaises(RuntimeError):
            with budget.operation_budget_scope(JOB):
                raise RuntimeError()
        self.assertIsNone(budget.current_budget())


from contextlib import contextmanager

if __name__ == '__main__':
    unittest.main()
