import ast
import copy
import json
from pathlib import Path
import re
import unittest
import test_transport_regressions as transport

ROOT = Path(__file__).resolve().parents[1]


class RetentionHold(Exception):
    pass


class CompletionMetadataTests(unittest.TestCase):
    def source(self):
        return transport.ProviderMetadataTests().source()

    def project(self, value):
        tree = ast.parse((ROOT / 'candidate/semantic_role_worker.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'project_diagnostic')
        scope = {'RetentionHold': RetentionHold, 're': re}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'actual_projector', 'exec'), scope)
        return scope['project_diagnostic'](value)

    def diagnostic(self, completion):
        return {
            'phase': 'reading_stream',
            'terminal_seen': False,
            'terminal_validated': False,
            'function_return_ready': False,
            'eof': True,
            'event_counts': {},
            'sequence_truncated': False,
            'provider_completion': completion,
        }

    def response(self):
        return {
            'id': 'gen-synthetic-123',
            'object': 'chat.completion',
            'model': 'deepseek/deepseek-v4-flash',
            'choices': [
                {
                    'index': 0,
                    'finish_reason': 'length',
                    'message': {'role': 'assistant', 'content': 'private text', 'reasoning': 'private chain'},
                }
            ],
            'usage': {'completion_tokens': 1600, 'completion_tokens_details': {'reasoning_tokens': 1500}},
        }

    def test_reason_and_usage_survive_without_any_content(self):
        meta = self.source()['completion_metadata'](self.response())
        result = self.project(self.diagnostic(meta))
        self.assertEqual(result['provider_completion']['finish_reason'], 'length')
        self.assertEqual(result['provider_completion']['completion_tokens'], 1600)
        self.assertEqual(result['provider_completion']['reasoning_tokens'], 1500)
        self.assertEqual(result['provider_completion']['provider_response_id'], 'gen-synthetic-123')
        self.assertNotIn('private', repr(result))

    def test_tool_refusal_role_are_separate_closed_facts(self):
        raw = self.response()
        raw['choices'][0]['finish_reason'] = 'tool_calls'
        raw['choices'][0]['message'].update(
            role='tool', tool_calls=[{'private': 'body'}], function_call={'private': 'body'}, refusal='private refusal'
        )
        result = self.project(self.diagnostic(self.source()['completion_metadata'](raw)))['provider_completion']
        self.assertEqual(result['message_role'], 'tool')
        self.assertTrue(result['tool_calls_present'] and result['function_call_present'] and result['refusal_present'])
        self.assertNotIn('private', repr(result))

    def test_unknown_strings_and_invalid_counts_cannot_escape(self):
        raw = self.response()
        raw['id'] = 'private credential'
        raw['choices'][0]['finish_reason'] = 'private message'
        raw['choices'][0]['message']['role'] = 'private role'
        raw['usage'] = {'completion_tokens': True, 'completion_tokens_details': {'reasoning_tokens': -1}}
        meta = self.source()['completion_metadata'](raw)
        self.assertEqual(meta['finish_reason'], 'other')
        self.assertEqual(meta['message_role'], 'other')
        self.assertIsNone(meta['provider_response_id'])
        self.assertIsNone(meta['completion_tokens'])
        self.assertIsNone(meta['reasoning_tokens'])
        self.assertNotIn('private', repr(meta))

    def test_actual_rejected_send_persists_finish_reason_and_usage(self):
        raw = json.dumps(self.response()).encode()
        diagnostics, requests = transport.ProviderMetadataTests().run_transport([raw], max_tokens=1600)
        self.assertEqual(len(requests), 1)
        self.assertTrue(diagnostics[0]['eof'])
        self.assertFalse(diagnostics[0]['terminal_validated'])
        projected = self.project(diagnostics[0])
        self.assertEqual(projected['provider_completion']['finish_reason'], 'length')
        self.assertEqual(projected['provider_completion']['reasoning_tokens'], 1500)
        self.assertNotIn('private text', repr(projected))

    def test_invalid_choice_index_and_message_shape_are_recorded(self):
        for index in (True, None, 1):
            raw = self.response()
            raw['choices'][0]['index'] = index
            meta = self.source()['completion_metadata'](raw)
            self.assertFalse(self.project(self.diagnostic(meta))['provider_completion']['choice_index_valid'])
        raw = self.response()
        raw['choices'][0]['message'] = []
        meta = self.source()['completion_metadata'](raw)
        self.assertFalse(self.project(self.diagnostic(meta))['provider_completion']['message_is_object'])

    def test_legacy_metadata_and_new_envelope_fields_are_validated(self):
        meta = self.source()['completion_metadata'](self.response())
        legacy = {
            k: v for k, v in meta.items() if k not in {'response_object', 'root_error_present', 'choice_error_present'}
        }
        self.assertEqual(self.project(self.diagnostic(legacy))['provider_completion'], legacy)
        for change in (
            {'response_object': 'private'},
            {'response_object': {}},
            {'root_error_present': 1},
            {'choice_error_present': 'private'},
        ):
            with self.subTest(change=tuple(change)), self.assertRaises(RetentionHold):
                self.project(self.diagnostic({**meta, **change}))

    def test_projector_rejects_extra_payload_or_wrong_types(self):
        meta = self.source()['completion_metadata'](self.response())
        for extra in (
            {'text': 'private'},
            {'completion_tokens': True},
            {'reasoning_tokens': 1000001},
            {'choice_count': 65},
            {'provider_response_id': 'Bearer private'},
            {'finish_reason': 'private'},
            {'tool_calls_present': 1},
            {'choice_index_valid': 1},
            {'message_is_object': 'private'},
        ):
            with self.subTest(change=tuple(extra)), self.assertRaises(RetentionHold):
                self.project(self.diagnostic({**meta, **extra}))


if __name__ == '__main__':
    unittest.main()
