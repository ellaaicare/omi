import json, unittest
import test_transport_regressions as transport
import test_completion_metadata as metadata


class InstructionJsonTests(unittest.TestCase):
    def raw(self, text):
        return json.dumps(
            {
                'model': 'deepseek/deepseek-v4-flash',
                'id': 'gen-synthetic-shape',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}],
            }
        ).encode()

    def test_instruction_control_is_not_buried_only_in_user_json(self):
        ds, calls = transport.ProviderMetadataTests().run_transport(
            [self.raw('{}')], expect_success=True, max_tokens=4096
        )
        body = calls[0][1]['json']
        messages = body['messages']
        wire = json.loads(messages[1]['content'])
        self.assertEqual(messages[0], {'role': 'system', 'content': wire['instructions']})
        self.assertEqual(messages[1]['role'], 'user')
        self.assertEqual(wire['content'], 'synthetic fixture')
        self.assertEqual(body['max_tokens'], 4096)
        self.assertEqual(body['reasoning'], {'effort': 'none'})
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(len(calls), 1)

    def test_content_free_failure_categories_do_not_accept_fences_or_scalars(self):
        for text, kind in [
            ('```json\n{"private":"DO_NOT_EXPORT"}\n```', 'fenced'),
            ('[]', 'array'),
            ('"DO_NOT_EXPORT"', 'string'),
            ('42', 'number'),
            ('true', 'boolean'),
            ('null', 'null'),
            ('{"x":1,"x":2}', 'duplicate_key'),
            ('{"x":NaN}', 'nonfinite'),
            ('{"private":', 'syntax_invalid'),
        ]:
            with self.subTest(kind=kind):
                ds, calls = transport.ProviderMetadataTests().run_transport([self.raw(text)], max_tokens=4096)
                self.assertEqual(ds[0]['provider_completion']['output_json_status'], kind)
                self.assertFalse(ds[0]['terminal_validated'])
                self.assertEqual(len(calls), 1)
                self.assertNotIn('DO_NOT_EXPORT', repr(ds))
                metadata.CompletionMetadataTests().project(
                    metadata.CompletionMetadataTests().diagnostic(ds[0]['provider_completion'])
                )

    def test_valid_object_still_requires_complete_validated_terminal(self):
        ds, calls = transport.ProviderMetadataTests().run_transport(
            [self.raw('{"a":1}')], expect_success=True, max_tokens=4096
        )
        self.assertEqual(ds[0]['provider_completion']['output_json_status'], 'object')
        self.assertTrue(ds[0]['terminal_validated'])
        self.assertEqual(len(calls), 1)

    def test_optional_shape_diagnostic_remains_closed_and_backward_compatible(self):
        m = metadata.CompletionMetadataTests()
        c = m.source()['completion_metadata'](json.loads(self.raw('{}')))
        m.project(m.diagnostic(c))
        for bad in ['DO_NOT_EXPORT', {}, [], True, 1, None]:
            with self.subTest(kind=type(bad).__name__):
                value = {**c, 'output_json_status': bad}
                with self.assertRaises(metadata.RetentionHold):
                    m.project(m.diagnostic(value))


if __name__ == '__main__':
    unittest.main()
