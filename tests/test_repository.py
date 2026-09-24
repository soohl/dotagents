"""Keep the public tree small and exclude local state."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepositoryTests(unittest.TestCase):
    def test_private_paths_are_ignored(self):
        for path in ['.env', '.env.backup', 'docs/notes.md', '.local/token',
                     'models/weights.gguf', 'build/cache', 'results/run.json',
                     'benchmarks/run.json', 'private.key', 'private.pfx',
                     'webui.db', 'webui.db-wal', 'chat.sqlite', 'chat.sqlite-shm',
                     'chat.sqlite3', 'chat.sqlite3-journal', '.coverage',
                     '.coverage.worker', 'htmlcov/index.html']:
            result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, path)

    def test_public_configuration_and_model_placeholder_are_not_ignored(self):
        for path in ['config/env.example', 'config/models.json', 'models/.gitkeep']:
            result = subprocess.run(['git', 'check-ignore', '--no-index', '-q', path], cwd=ROOT)
            self.assertEqual(result.returncode, 1, path)

    def test_no_ignored_paths_are_tracked(self):
        names = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--ignored', '--exclude-standard'],
            cwd=ROOT, text=True)
        self.assertEqual(names, '')

    def test_publishable_tree_has_no_local_paths_or_private_material(self):
        names = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                        cwd=ROOT).decode().split('\0')
        for name in set(names):
            path = ROOT / name
            if not name or not path.is_file():
                continue
            self.assertNotIn(name.split('/')[0], ['docs', '.local', 'benchmarks', 'results'])
            if path.suffix in ('.png', '.jpg', '.webp'):
                continue
            text = path.read_text()
            self.assertIsNone(re.search(r'/(?:Users|home)/[A-Za-z0-9_-]+/', text), name)
            self.assertIsNone(re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text), name)
            self.assertIsNone(re.search(r'\b(?:sk-[A-Za-z0-9]{24,}|BSA[A-Za-z0-9_-]{24,})\b', text), name)


if __name__ == '__main__':
    unittest.main()
