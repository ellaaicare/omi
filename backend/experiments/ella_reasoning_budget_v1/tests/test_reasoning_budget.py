"""Wire-only reasoning correction; fakeHTTP, no provider/credential/DB access."""

import ast
import json
from pathlib import Path
import unittest
import test_transport_regressions as transport

ROOT = Path(__file__).resolve().parents[1]


class ReasoningBudgetTests(unittest.TestCase):
    def response(self, finish='stop', content='{}', reasoning=0):
        return json.dumps(
            {
                'id': 'gen-synthetic-budget',
                'model': 'deepseek/deepseek-v4-flash',
                'choices': [
                    {'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant', 'content': content}}
                ],
                'usage': {
                    'prompt_tokens': 12,
                    'completion_tokens': 1600 if finish == 'length' else 20,
                    'completion_tokens_details': {'reasoning_tokens': reasoning},
                },
            }
        ).encode()

    def test_request_explicitly_disables_optional_reasoning_without_increasing_cap(self):
        ds, calls = transport.ProviderMetadataTests().run_transport(
            [self.response()], expect_success=True, max_tokens=1600
        )
        body = calls[0][1]['json']
        self.assertEqual(body['reasoning'], {'effort': 'none'})
        self.assertEqual(body['max_tokens'], 1600)
        self.assertEqual(body['model'], 'deepseek/deepseek-v4-flash')
        self.assertEqual(body['tools'], [])
        self.assertFalse(body['stream'])
        self.assertEqual(body['provider'], {'allow_fallbacks': False, 'data_collection': 'deny'})
        self.assertEqual(len(calls), 1)
        self.assertTrue(ds[0]['terminal_validated'])

    def test_provider_ignoring_setting_length_still_holds_without_retry(self):
        ds, calls = transport.ProviderMetadataTests().run_transport(
            [self.response('length', '', 1596)], max_tokens=1600
        )
        self.assertEqual(len(calls), 1)
        self.assertFalse(ds[0]['terminal_validated'])
        self.assertEqual(ds[0]['provider_completion']['finish_reason'], 'length')
        self.assertEqual(ds[0]['provider_completion']['reasoning_tokens'], 1596)
        self.assertEqual(ds[0]['provider_completion']['provider_response_id'], 'gen-synthetic-budget')

    def test_tools_are_not_treated_as_success_or_executed(self):
        value = json.loads(self.response())
        value['choices'][0]['message']['tool_calls'] = [
            {'id': 'synthetic', 'type': 'function', 'function': {'name': 'synthetic', 'arguments': '{}'}}
        ]
        ds, calls = transport.ProviderMetadataTests().run_transport([json.dumps(value).encode()], max_tokens=1600)
        self.assertEqual(len(calls), 1)
        self.assertFalse(ds[0]['terminal_validated'])

    def test_only_transport_body_setting_changes_from_serving_baseline(self):
        before = ast.parse((ROOT / 'baseline/semantic_openrouter_transport.py.source').read_text())
        after = ast.parse((ROOT / 'candidate/semantic_openrouter_transport.py').read_text())
        changed = []
        for node in ast.walk(after):
            if isinstance(node, ast.Dict):
                indexes = [i for i, k in enumerate(node.keys) if isinstance(k, ast.Constant) and k.value == 'reasoning']
                if indexes:
                    i = indexes[0]
                    self.assertEqual(ast.literal_eval(node.values[i]), {'effort': 'none'})
                    del node.keys[i]
                    del node.values[i]
                    changed.append(node)
        self.assertEqual(len(changed), 1)
        self.assertEqual(ast.dump(before), ast.dump(after))

    def test_capability_supports_disabled_reasoning_for_exact_model(self):
        v = json.loads((ROOT / 'evidence/PUBLIC.MODEL.REASONING.CAPABILITY.safe.json').read_text())['model']
        self.assertEqual(v['id'], 'deepseek/deepseek-v4-flash')
        self.assertFalse(v['reasoning']['mandatory'])
        self.assertIn('none', v['reasoning']['supported_efforts'])
        self.assertEqual(v['canonical_slug'], 'deepseek/deepseek-v4-flash-20260423')


if __name__ == '__main__':
    unittest.main()
