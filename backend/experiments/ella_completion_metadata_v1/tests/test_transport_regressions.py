"""Actual transport source; fake client/snapshot/parser, no sockets or credentials."""

import ast
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import signal
import time
import types
import unittest
from unittest.mock import patch
import httpx

ROOT = Path(__file__).resolve().parents[1]


class IsolationHold(Exception):
    pass


class ProviderMetadataTests(unittest.TestCase):
    def source(self):
        source = (ROOT / 'candidate/semantic_openrouter_transport.py').read_text()
        tree = ast.parse(source)
        tree.body = [
            node
            for node in tree.body
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module in {'dotenv.parser', 'gateway.platforms.semantic_codex_transport'}
            )
        ]
        scope = {
            'IsolationHold': IsolationHold,
            '_read_snapshot': lambda _: b'SYNTHETIC',
            'parse_stream': lambda _: [
                types.SimpleNamespace(key='OPENROUTER_API_KEY', error=False, value='synthetic-unusable-key')
            ],
        }
        exec(compile(tree, 'actual_semantic_openrouter_transport.py', 'exec'), scope)
        return scope

    def run_transport(self, parts, failure=None, *, absolute=False, expect_success=False, max_tokens=8192):
        module = self.source()
        diagnostics = []
        requests = []
        request = {'policy': 'synthetic', 'content': 'synthetic fixture'}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        class Response:
            status_code = 200
            headers = {'content-type': 'application/json'}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_raw(self):
                yield from parts
                if absolute:
                    time.sleep(0.1)
                if failure:
                    raise failure

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def stream(self, *args, **kwargs):
                requests.append((args, kwargs))
                return Response()

        original_deadline = module['_deadline']
        if absolute:
            module['_deadline'] = lambda: original_deadline(0.02)
        with patch.object(httpx, 'Client', Client):
            send = module['build_isolated_client'](
                auth_path=Path('/synthetic/auth.json'),
                authority_digest='a' * 64,
                revalidate_authority=lambda: 'a' * 64,
                approved_request_hashes=frozenset({request_hash}),
                diagnostic_observer=lambda d: diagnostics.append(json.loads(json.dumps(d))),
                max_output_tokens=max_tokens,
            )
            if expect_success:
                result = send(request)
                self.assertEqual(result['model'], module['MODEL'])
            else:
                with self.assertRaises(IsolationHold):
                    send(request)
            with self.assertRaises(IsolationHold):
                send(request)  # never opens a second provider request
        return diagnostics, requests

    def test_partial_bytes_and_read_timeout_are_retained_without_content(self):
        private = b'private fixture bytes'
        diagnostics, requests = self.run_transport([private, b'123'], httpx.ReadTimeout('private endpoint'))
        self.assertEqual(len(requests), 1)
        d = diagnostics[0]
        self.assertEqual(d['protocol_metadata']['wire_bytes'], len(private) + 3)
        self.assertEqual(d['protocol_metadata']['decoded_bytes'], len(private) + 3)
        self.assertFalse(d['eof'])
        self.assertFalse(d['terminal_validated'])
        self.assertEqual(d['exception'], [{'class': 'ReadTimeout', 'locations': []}])
        self.assertNotIn('private', repr(d))

    def test_absolute_deadline_origin_is_distinct_and_timer_restored(self):
        before = signal.getsignal(signal.SIGALRM)
        diagnostics, requests = self.run_transport([b'1'], absolute=True)
        self.assertEqual(diagnostics[0]['exception'], [{'class': 'TimeoutError', 'locations': []}])
        self.assertEqual(diagnostics[0]['protocol_metadata']['wire_bytes'], 1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(len(requests), 1)

    def test_connect_and_protocol_errors_remain_distinct(self):
        for cls in (httpx.ConnectTimeout, httpx.RemoteProtocolError):
            with self.subTest(error=cls.__name__):
                diagnostics, _ = self.run_transport([], cls('private URL'))
                self.assertEqual(diagnostics[0]['exception'][0]['class'], cls.__name__)
                self.assertFalse(diagnostics[0]['function_return_ready'])

    def test_absolute_sentinel_survives_real_httpcore_socket_mapping(self):
        import socket
        import httpcore
        from httpcore._exceptions import map_exceptions

        module = self.source()
        sentinel = module['ProviderDeadlineExceeded']
        with self.assertRaises(sentinel):
            with map_exceptions({socket.timeout: httpcore.ReadTimeout, OSError: httpcore.ReadError}):
                raise sentinel()
        # The origin would be lost if the sentinel inherited TimeoutError/OSError.
        with self.assertRaises(httpcore.ReadTimeout):
            with map_exceptions({socket.timeout: httpcore.ReadTimeout, OSError: httpcore.ReadError}):
                raise TimeoutError()

    def test_caller_cap_reaches_provider_and_long_json_is_not_truncated(self):
        content = json.dumps({'overview': 'synthetic ' * 900, 'quotes': ['synthetic quote'] * 50})
        raw = json.dumps(
            {
                'model': 'deepseek/deepseek-v4-flash',
                'id': 'synthetic-provider-id',
                'choices': [
                    {'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}
                ],
            }
        ).encode()
        diagnostics, requests = self.run_transport([raw[:50], raw[50:]], expect_success=True, max_tokens=1600)
        self.assertEqual(requests[0][1]['json']['max_tokens'], 1600)
        self.assertTrue(diagnostics[0]['terminal_validated'])
        self.assertEqual(diagnostics[0]['protocol_metadata']['wire_bytes'], len(raw))
        self.assertNotIn(content, repr(diagnostics))

    def test_token_limit_finish_and_incomplete_json_remain_rejected(self):
        for finish, content in [('length', '{}'), ('stop', '{"truncated":')]:
            with self.subTest(finish=finish):
                raw = json.dumps(
                    {
                        'model': 'deepseek/deepseek-v4-flash',
                        'id': 'synthetic-provider-id',
                        'choices': [
                            {'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant', 'content': content}}
                        ],
                    }
                ).encode()
                diagnostics, requests = self.run_transport([raw], max_tokens=1600)
                self.assertFalse(diagnostics[0]['terminal_validated'])
                self.assertEqual(len(requests), 1)

    def test_empty_http_200_is_not_success(self):
        diagnostics, requests = self.run_transport([])
        self.assertEqual(len(requests), 1)
        self.assertTrue(diagnostics[0]['eof'])
        self.assertFalse(diagnostics[0]['terminal_validated'])


if __name__ == '__main__':
    unittest.main()
