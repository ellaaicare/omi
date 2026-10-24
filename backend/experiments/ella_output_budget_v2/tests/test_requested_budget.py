import ast, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / 'ella_reasoning_budget_v1/tests'))
import test_transport_regressions as transport


class RequestedBudgetTests(unittest.TestCase):
    def cap(self):
        t = ast.parse((ROOT / 'candidate/semantic_self_hosted_runtime.py').read_text())
        caps = [
            v.value
            for n in ast.walk(t)
            if isinstance(n, ast.Dict)
            for k, v in zip(n.keys, n.values)
            if isinstance(k, ast.Constant) and k.value == 'max_tokens' and isinstance(v, ast.Constant)
        ]
        self.assertEqual(caps, [4096])
        return caps[0]

    def test_actual_caller_budget_reaches_actual_transport(self):
        raw = json.dumps(
            {
                'model': 'deepseek/deepseek-v4-flash',
                'id': 'gen-synthetic-budget-4096',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{}'}}],
            }
        ).encode()
        ds, calls = transport.ProviderMetadataTests().run_transport([raw], expect_success=True, max_tokens=self.cap())
        self.assertEqual(calls[0][1]['json']['max_tokens'], 4096)
        self.assertEqual(calls[0][1]['json']['reasoning'], {'effort': 'none'})
        self.assertEqual(len(calls), 1)

    def test_larger_cap_never_accepts_length(self):
        raw = json.dumps(
            {
                'model': 'deepseek/deepseek-v4-flash',
                'id': 'gen-synthetic-budget-4096',
                'choices': [{'index': 0, 'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{}'}}],
            }
        ).encode()
        ds, calls = transport.ProviderMetadataTests().run_transport([raw], max_tokens=self.cap())
        self.assertFalse(ds[0]['terminal_validated'])
        self.assertEqual(len(calls), 1)

    def test_only_requested_budget_changes(self):
        a = ast.parse((ROOT / 'baseline/semantic_self_hosted_runtime.py.source').read_text())
        b = ast.parse((ROOT / 'candidate/semantic_self_hosted_runtime.py').read_text())
        changed = 0
        for n in ast.walk(b):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if isinstance(k, ast.Constant) and k.value == 'max_tokens':
                        self.assertEqual(v.value, 4096)
                        v.value = 1600
                        changed += 1
        self.assertEqual(changed, 1)
        self.assertEqual(ast.dump(a), ast.dump(b))


if __name__ == '__main__':
    unittest.main()
