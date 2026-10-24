"""Actual execute() with synthetic profile/parser/factory; no external adapters."""

import ast
import hashlib
import json
from pathlib import Path
import sys
import time
import types
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SemanticRoleHold(Exception):
    pass


class OutputBudgetTests(unittest.TestCase):
    def invoke(self, limit):
        path = ROOT / 'host-candidate/semantic_direct_role.py'
        tree = ast.parse(path.read_text())
        execute = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'execute')
        seen = []
        config = {'model': {'provider': 'openrouter', 'default': 'deepseek/deepseek-v4-flash'}}
        policy = {'version': 'synthetic', 'digest': 'b' * 64}

        def factory(**kwargs):
            seen.append(kwargs)
            self.assertEqual(kwargs['revalidate_authority'](), hashlib.sha256(b'synthetic').hexdigest())
            return lambda _: {
                'model': config['model']['default'],
                'provider_response_id': 'synthetic-result',
                'text': '{}',
            }

        scope = {
            'SemanticRoleHold': SemanticRoleHold,
            '_read_config_snapshot': lambda _: b'synthetic',
            'require_shared_root_policy': lambda *_: policy,
            'hashlib': hashlib,
            'json': json,
            'uuid': uuid,
            'time': time,
            'ENDPOINT': 'https://synthetic.invalid/responses',
            'build_isolated_client': lambda **_: (_ for _ in ()).throw(AssertionError('wrong provider')),
        }
        exec(compile(ast.Module(body=[execute], type_ignores=[]), str(path), 'exec'), scope)
        yaml = types.ModuleType('yaml')
        yaml.safe_load = lambda _: config
        provider = types.ModuleType('gateway.platforms.semantic_openrouter_transport')
        provider.build_isolated_client = factory
        request = {
            'stream': False,
            'model': 'synthetic-agent',
            'max_tokens': limit,
            'messages': [
                {
                    'role': 'user',
                    'content': json.dumps({'policy': 'ella-semantic-summary-v1', 'instructions': 'synthetic fixture'}),
                }
            ],
        }
        with patch.dict(sys.modules, {'yaml': yaml, 'gateway.platforms.semantic_openrouter_transport': provider}):
            result = scope['execute'](
                request_body=request,
                session='synthetic',
                role='generator',
                nonce='a' * 64,
                profile='synthetic-profile',
                auth_path='/synthetic/auth.json',
                config_path='/synthetic/config.yaml',
                revalidate_config=lambda: ('synthetic-profile', '/synthetic/auth.json', '/synthetic/config.yaml'),
            )
        return seen, result

    def test_existing_caller_budget_is_forwarded_without_expansion(self):
        for limit in (1, 1600, 8192):
            with self.subTest(limit=limit):
                seen, result = self.invoke(limit)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0]['max_output_tokens'], limit)
                self.assertTrue(result['semantic_protocol']['completed'])

    def test_invalid_budget_rejects_before_provider_factory(self):
        for limit in (None, True, False, 0, -1, 8193, 1600.0, '1600'):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(SemanticRoleHold, '^semantic_role_request_invalid$'):
                    self.invoke(limit)


if __name__ == '__main__':
    unittest.main()
