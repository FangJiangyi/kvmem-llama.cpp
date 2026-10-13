import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('ci_changes', Path(__file__).resolve().parents[1] /
                                            '.github/scripts/classify_changes.py')
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)


class DocumentationChangesTest(unittest.TestCase):
    def test_documentation_and_images_skip_builds(self):
        self.assertFalse(changes.requires_ci(['README.md', 'docs/guide.md', 'docs/assets/flow.svg',
                                             'scripts/windows/README-quantizer.md', 'LICENSE']))

    def test_code_configurations_and_submodules_always_run(self):
        for path in ('src/main.cpp', 'docs/example.py', 'docs/config.json', 'CMakeLists.txt',
                     'VERSION', '.github/workflows/server-compat.yml',
                     '.github/scripts/classify_changes.py', 'backends/ninfer',
                     'backends/versions.json', 'scripts/windows/start-ninfer.ps1',
                     'tests/fixtures/answer.md'):
            with self.subTest(path=path):
                self.assertTrue(changes.requires_ci(['README.md', path]))

    def test_empty_unknown_and_manual_events_run(self):
        self.assertTrue(changes.requires_ci([]))
        self.assertTrue(changes.classify('workflow_dispatch', {})[0])
        self.assertTrue(changes.classify('unknown', {})[0])
        self.assertTrue(changes.classify('pull_request', {})[0])
        self.assertTrue(changes.classify('push', {'before': '0' * 40, 'after': '1' * 40})[0])

    def test_real_git_diff_renames_and_diverged_pull_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE).decode().strip()

            def commit(message):
                git('add', '.')
                git('-c', 'user.name=CI test', '-c', 'user.email=ci-test@example.invalid',
                    'commit', '-qm', message)
                return git('rev-parse', 'HEAD')

            git('init', '-q', '-b', 'test-base')
            (root / 'README.md').write_text('base\n')
            (root / 'main.cpp').write_text('int main() {}\n')
            base = commit('base')
            (root / 'README.md').write_text('updated documentation\n')
            docs = commit('documentation')
            event = {'pull_request': {'base': {'sha': base}, 'head': {'sha': docs}}}
            self.assertFalse(changes.classify('pull_request', event, root)[0])
            self.assertFalse(changes.classify('push', {'before': base, 'after': docs}, root)[0])
            git('checkout', '-q', '-b', 'base-update', base)
            (root / 'other.cpp').write_text('int other;\n')
            advanced_base = commit('base advances independently')
            event['pull_request']['base']['sha'] = advanced_base
            self.assertFalse(changes.classify('pull_request', event, root)[0])
            git('checkout', '-q', 'test-base')
            (root / 'main.cpp').rename(root / 'example.md')
            renamed = commit('move source into markdown')
            full, _, paths = changes.classify('push', {'before': docs, 'after': renamed}, root)
            self.assertTrue(full)
            self.assertIn('main.cpp', paths)
            self.assertIn('example.md', paths)
            self.assertTrue(changes.classify('push', {'before': 'f' * 40, 'after': renamed}, root)[0])


if __name__ == '__main__':
    unittest.main()
