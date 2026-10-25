import ast, importlib.util, json, unittest, hashlib
from pathlib import Path
from google.api_core import exceptions

R = Path(__file__).resolve().parents[1]


def load(path):
    s = importlib.util.spec_from_file_location('diag_' + path.parent.name, path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


D = load(R / 'candidate/causal_diagnostics.py')
B = load(R / 'baseline/causal_diagnostics.py')
source = (R.parent / 'ella_queue_resilience_v4/candidate/ella/services/summary_writeback.py').read_text()
assert hashlib.sha256(source.encode()).hexdigest() == '2377762a6c251521cdf81901a9307b33ae61c6de634ec430a2438a214331cae8'
tree = ast.parse(source)
classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ConversationSummaryOutcomeUnknownError']
ns = {}
exec(compile(ast.Module(body=classes, type_ignores=[]), 'summary_writeback.py', 'exec'), ns)
Unknown = ns['ConversationSummaryOutcomeUnknownError']
SECRET = 'PRIVATE transcript bearer secret-key'


def chain():
    try:
        try:
            raise exceptions.DeadlineExceeded(SECRET)
        except exceptions.DeadlineExceeded as inner:
            raise exceptions.RetryError(SECRET, inner) from inner
    except exceptions.RetryError as inner:
        try:
            raise Unknown('conversation_summary_outcome_unknown') from inner
        except Unknown as outer:
            return outer


class Tests(unittest.TestCase):
    def test_baseline_reproduces_three_masked_types(self):
        self.assertEqual(B.safe_exception(chain(), 'normal_enrichment_dispatch')['cause_types'], ['OtherException'] * 3)

    def test_actual_google_classes_and_actual_writeback_class_survive(self):
        v = D.safe_exception(chain(), 'normal_enrichment_dispatch')
        self.assertEqual(v['cause_types'], ['ConversationSummaryOutcomeUnknownError', 'RetryError', 'DeadlineExceeded'])
        self.assertEqual(v['cause_metadata'][0]['closed_code'], 'conversation_summary_outcome_unknown')
        self.assertNotIn(SECRET, json.dumps(v))
        self.assertEqual(D.sanitize_diagnostic(v), v)

    def test_no_arbitrary_unknown_message_export(self):
        e = Unknown(SECRET)
        v = D.safe_exception(e, 'normal_enrichment_dispatch')
        self.assertNotIn('closed_code', v['cause_metadata'][0])
        self.assertNotIn(SECRET, json.dumps(v))

    def test_all_added_google_labels_strip_messages(self):
        for typ in [
            exceptions.Aborted,
            exceptions.DeadlineExceeded,
            exceptions.FailedPrecondition,
            exceptions.ServiceUnavailable,
            exceptions.InvalidArgument,
        ]:
            with self.subTest(typ=typ.__name__):
                v = D.safe_exception(typ(SECRET), 'normal_enrichment_dispatch')
                self.assertEqual(v['exception_type'], typ.__name__)
                self.assertNotIn(SECRET, json.dumps(v))
                self.assertEqual(D.sanitize_diagnostic(v), v)

    def test_unlisted_type_stays_closed(self):
        class PrivateException(Exception):
            pass

        self.assertEqual(
            D.safe_exception(PrivateException(SECRET), 'normal_enrichment_dispatch')['exception_type'], 'OtherException'
        )

    def test_extra_fields_and_private_codes_rejected(self):
        v = D.safe_exception(chain(), 'normal_enrichment_dispatch')
        self.assertIsNone(D.sanitize_diagnostic({**v, 'private': SECRET}))
        v['cause_metadata'][0]['closed_code'] = SECRET
        self.assertIsNone(D.sanitize_diagnostic(v))

    def test_actual_source_frame_without_message_or_local_export(self):
        ns = {'Unknown': Unknown}
        exec(
            compile(
                'def apply():\n raise Unknown("conversation_summary_outcome_unknown")\n',
                'semantic_retained_apply.py',
                'exec',
            ),
            ns,
        )
        try:
            ns['apply']()
        except Unknown as e:
            v = D.safe_exception(e, 'normal_enrichment_dispatch')
        self.assertEqual(
            v['cause_metadata'][0]['frames'], [{'file': 'semantic_retained_apply.py', 'function': 'apply', 'line': 2}]
        )
        self.assertNotIn('locals', json.dumps(v))

    def test_all_function_asts_unchanged(self):
        def funcs(p):
            return {
                n.name: ast.dump(n, include_attributes=False)
                for n in ast.parse(p.read_text()).body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }

        self.assertEqual(funcs(R / 'baseline/causal_diagnostics.py'), funcs(R / 'candidate/causal_diagnostics.py'))


if __name__ == '__main__':
    unittest.main()
