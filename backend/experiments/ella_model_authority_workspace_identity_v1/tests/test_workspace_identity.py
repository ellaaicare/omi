import importlib.util, importlib.machinery, unittest, tempfile, pathlib, os, json, hashlib, shutil

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(path, name):
    s = importlib.util.spec_from_file_location(name, path, loader=importlib.machinery.SourceFileLoader(name, str(path)))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


BASE = ROOT / 'baseline/semantic_runtime_authority.py.source'
NEW = ROOT / 'candidate/semantic_runtime_authority.py.source'


class Tests(unittest.TestCase):
    def exercise(self, path, mutation, file_mutation=None):
        m = load(path, 'fixture_authority')
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td).resolve()
            os.chmod(root, 0o700)
            profiles = root / 'profiles'
            profiles.mkdir(mode=0o700)
            profile = profiles / 'synthetic-profile'
            profile.mkdir(mode=0o700)
            workspace = profile / 'workspace'
            workspace.mkdir(mode=0o700)
            uid = 'synthetic-owner'
            account = 'synthetic-account'
            agent = 'synthetic-agent'
            registry = {
                'agent_owners': {agent: uid, profile.name: uid},
                'agent_workspaces': {agent: str(workspace), profile.name: str(workspace)},
                'users': {
                    account: {
                        'omiUid': uid,
                        'userId': account,
                        'hermes': {'userAgentId': agent, 'userProfile': profile.name, 'userWorkspace': str(workspace)},
                    }
                },
            }
            policy = {
                'version': 'synthetic-v1',
                'provider': 'openai-codex',
                'model': 'gpt-6.1-sol',
                'credentialRef': 'global-auth:openai-codex',
                'fallbackModels': [],
                'reasoningEffort': 'medium',
                'apiToolsets': [],
                'isolationPosture': 'synthetic',
            }
            policy['digest'] = hashlib.sha256(
                json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()
            ).hexdigest()
            config = {'model': {'default': 'gpt-6.1-sol', 'provider': 'openai-codex'}, 'ella_runtime_policy': policy}
            for p, data in [
                (root / 'registry.json', registry),
                (profile / 'config.yaml', config),
                (
                    profile / 'ella-profile.json',
                    {
                        'schemaVersion': 1,
                        'profile': profile.name,
                        'uid': uid,
                        'userId': account,
                        'role': 'user',
                        'workspace': str(workspace),
                    },
                ),
            ]:
                p.write_text(json.dumps(data))
                p.chmod(0o600)
            original = m.directory_identity
            reads = 0

            def directory(path, deadline):
                nonlocal reads
                if pathlib.Path(path) == workspace:
                    reads += 1
                    if reads == 2:
                        mutation(root, profile, workspace)
                return original(path, deadline)

            m.directory_identity = directory
            original_read = m.read_file
            file_counts = {}

            def read(path, deadline):
                path = pathlib.Path(path)
                file_counts[path.name] = file_counts.get(path.name, 0) + 1
                if file_mutation is not None and file_counts[path.name] == 2:
                    file_mutation(path)
                return original_read(path, deadline)

            m.read_file = read
            return m.runtime_authority(
                body={
                    'uid': uid,
                    'agent_id': agent,
                    'account_user_id': account,
                    'legacy_runtime_identity_sha256': 'a' * 64,
                },
                request_owner_uid=uid,
                agent_id=agent,
                registry_path=root / 'registry.json',
                profiles_root=profiles,
            )

    def test_baseline_reproduces_timestamp_only_rejection(self):
        with self.assertRaisesRegex(RuntimeError, 'semantic_authority_sources_changed'):
            self.exercise(BASE, lambda r, p, w: os.utime(w, ns=(1000000000, 2000000000)))

    def test_candidate_timestamp_only_passes(self):
        self.assertEqual(
            self.exercise(NEW, lambda r, p, w: os.utime(w, ns=(1000000000, 2000000000)))['model'], 'gpt-6.1-sol'
        )

    def test_atomic_workspace_file_change_passes(self):
        def mutation(r, p, w):
            t = w / 'synthetic.tmp'
            t.write_text('fictional')
            t.replace(w / 'synthetic.txt')

        self.assertEqual(self.exercise(NEW, mutation)['profile_name'], 'synthetic-profile')

    def test_replaced_directory_rejects(self):
        def mutation(r, p, w):
            w.rename(p / 'old-workspace')
            w.mkdir(mode=0o700)

        with self.assertRaisesRegex(RuntimeError, 'semantic_authority_sources_changed'):
            self.exercise(NEW, mutation)

    def test_unsafe_mode_rejects(self):
        with self.assertRaisesRegex(RuntimeError, 'semantic_authority_directory_unsafe'):
            self.exercise(NEW, lambda r, p, w: w.chmod(0o777))

    def test_safe_mode_change_rejects(self):
        with self.assertRaisesRegex(RuntimeError, 'semantic_authority_sources_changed'):
            self.exercise(NEW, lambda r, p, w: w.chmod(0o500))

    def test_foreign_workspace_symlink_rejects(self):
        def mutation(r, p, w):
            w.rename(p / 'old-workspace')
            w.symlink_to(p / 'old-workspace', target_is_directory=True)

        with self.assertRaises(OSError):
            self.exercise(NEW, mutation)

    def test_all_authority_file_changes_reject(self):
        for target in ['registry.json', 'config.yaml', 'ella-profile.json']:

            def change(path):
                if path.name == target:
                    path.write_bytes(path.read_bytes() + b' ')

            with self.subTest(target=target), self.assertRaisesRegex(
                RuntimeError, 'semantic_authority_sources_changed'
            ):
                self.exercise(NEW, lambda *args: None, file_mutation=change)

    def test_file_content_guard_preserved(self):
        m = load(NEW, 'fixture_authority')
        original = m.read_file
        count = 0
        # Structural guarantee: full source byte comparisons remain before directory checks.
        import ast

        base = ast.parse(BASE.read_text())
        new = ast.parse(NEW.read_text())
        # Exact source delta is one closed component predicate, not removed guards.
        expected = BASE.read_text().replace(
            'if component == "registry_parent_directory" and type(current) is tuple and type(expected) is tuple',
            'if component in ("registry_parent_directory", "workspace_directory") and type(current) is tuple and type(expected) is tuple',
        )
        self.assertEqual(NEW.read_text(), expected)


if __name__ == '__main__':
    unittest.main()
