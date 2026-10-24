"""Documented error envelopes cannot masquerade as successful stop responses."""

import ast
import json
from pathlib import Path
import unittest
import types
from unittest.mock import patch
import test_transport_regressions as transport

ROOT = Path(__file__).resolve().parents[1]


class ChoiceErrorTests(unittest.TestCase):
    def response(self):
        return {
            'id': 'gen-synthetic-123',
            'object': 'chat.completion',
            'model': 'deepseek/deepseek-v4-flash',
            'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}],
        }

    def test_serving_baseline_reproduces_acceptance_of_choice_error(self):
        tree = ast.parse((ROOT / 'baseline/semantic_openrouter_transport.py.source').read_text())
        tree.body = [
            node
            for node in tree.body
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module in {'dotenv.parser', 'gateway.platforms.semantic_codex_transport'}
            )
        ]

        class IsolationHold(Exception):
            pass

        scope = {
            'IsolationHold': transport.IsolationHold,
            '_read_snapshot': lambda _: b'SYNTHETIC',
            'parse_stream': lambda _: [
                types.SimpleNamespace(key='OPENROUTER_API_KEY', error=False, value='synthetic-unusable-key')
            ],
        }
        exec(compile(tree, 'actual_serving_baseline', 'exec'), scope)
        value = self.response()
        value['choices'][0]['error'] = {'code': 502, 'message': 'private failure'}
        with patch.object(transport.ProviderMetadataTests, 'source', lambda _: scope):
            diagnostic, _ = transport.ProviderMetadataTests().run_transport(
                [json.dumps(value).encode()], expect_success=True, max_tokens=1600
            )
        self.assertTrue(diagnostic[0]['terminal_validated'])  # Existing bug, not an allowed new outcome.

    def test_choice_error_never_accepted_as_complete(self):
        for error in ({'code': 502, 'message': 'private provider failure'}, {}, False, 0, [], ''):
            with self.subTest(kind=type(error).__name__):
                value = self.response()
                value['choices'][0]['error'] = error
                diagnostic, calls = transport.ProviderMetadataTests().run_transport(
                    [json.dumps(value).encode()], max_tokens=1600
                )
                self.assertEqual(len(calls), 1)
                self.assertFalse(diagnostic[0]['terminal_validated'])
                self.assertTrue(diagnostic[0]['eof'])
                self.assertNotIn('private', repr(diagnostic))

    def test_root_error_non_null_never_accepted(self):
        for error in ({'code': 502, 'message': 'private'}, {}, False, 0, [], ''):
            with self.subTest(kind=type(error).__name__):
                value = self.response()
                value['error'] = error
                diagnostic, calls = transport.ProviderMetadataTests().run_transport(
                    [json.dumps(value).encode()], max_tokens=1600
                )
                self.assertEqual(len(calls), 1)
                self.assertFalse(diagnostic[0]['terminal_validated'])

    def test_absent_or_null_error_still_accepts_complete_stop(self):
        for present in (False, True):
            value = self.response()
            if present:
                value['error'] = None
                value['choices'][0]['error'] = None
            diagnostic, calls = transport.ProviderMetadataTests().run_transport(
                [json.dumps(value).encode()], expect_success=True, max_tokens=1600
            )
            self.assertTrue(diagnostic[0]['terminal_validated'])
            self.assertEqual(calls[0][1]['json']['max_tokens'], 1600)

    def test_wrong_or_missing_nonstream_object_is_rejected(self):
        for kind in (None, 'chat.completion.chunk', 'other', True, 0, {}):
            with self.subTest(kind=kind):
                value = self.response()
                if kind is None:
                    del value['object']
                else:
                    value['object'] = kind
                diagnostics, calls = transport.ProviderMetadataTests().run_transport(
                    [json.dumps(value).encode()], max_tokens=1600
                )
                self.assertFalse(diagnostics[0]['terminal_validated'])
                self.assertEqual(len(calls), 1)
                self.assertIn(
                    diagnostics[0]['provider_completion']['response_object'],
                    ['missing', 'chat.completion.chunk', 'other'],
                )

    def test_nonstop_and_tool_function_refusal_guards_remain(self):
        for reason in ('length', 'tool_calls', 'content_filter', 'error', None):
            value = self.response()
            value['choices'][0]['finish_reason'] = reason
            diagnostics, _ = transport.ProviderMetadataTests().run_transport(
                [json.dumps(value).encode()], max_tokens=1600
            )
            self.assertFalse(diagnostics[0]['terminal_validated'])
        for change in (
            {'tool_calls': [{'private': 'tool arguments'}]},
            {'function_call': {'private': 'function arguments'}},
            {'refusal': 'private refusal'},
        ):
            value = self.response()
            value['choices'][0]['message'].update(change)
            diagnostics, _ = transport.ProviderMetadataTests().run_transport(
                [json.dumps(value).encode()], max_tokens=1600
            )
            self.assertFalse(diagnostics[0]['terminal_validated'])
            self.assertNotIn('private', repr(diagnostics))

    def test_missing_index_remains_rejected(self):
        value = self.response()
        del value['choices'][0]['index']
        diagnostic, _ = transport.ProviderMetadataTests().run_transport([json.dumps(value).encode()], max_tokens=1600)
        self.assertFalse(diagnostic[0]['terminal_validated'])


if __name__ == '__main__':
    unittest.main()
