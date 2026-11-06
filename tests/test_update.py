import subprocess
import tempfile
import unittest
from pathlib import Path

from haplocaller import update


def git(cwd, *args):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args], cwd=cwd, check=True, capture_output=True)


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote, self.work, self.other = (self.tmp / n for n in ('remote.git', 'work', 'other'))
        git(self.tmp, 'init', '-q', '--bare', '-b', 'main', str(self.remote))
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.work))
        (self.work / 'a.txt').write_text('1')
        git(self.work, 'add', '.'); git(self.work, 'commit', '-qm', 'one'); git(self.work, 'push', '-q', 'origin', 'HEAD:main')
        git(self.work, 'branch', '-q', '--set-upstream-to=origin/main')
        git(self.tmp, 'clone', '-q', str(self.remote), str(self.other))
        (self.other / 'a.txt').write_text('2')
        git(self.other, 'commit', '-qam', 'two'); git(self.other, 'push', '-q', 'origin', 'HEAD:main')
        self.stamp = self.tmp / 'stamp'

    def test_behind_clean_updates(self):
        self.assertEqual(update.check(self.work, self.stamp), 1)
        self.assertTrue(update.apply(self.work))
        self.assertEqual((self.work / 'a.txt').read_text(), '2')

    def test_checked_today_is_silent(self):
        self.assertEqual(update.check(self.work, self.stamp), 1)
        self.assertEqual(update.check(self.work, self.stamp), 0)

    def test_dirty_tree_is_left_alone(self):
        (self.work / 'a.txt').write_text('mine')
        self.assertEqual(update.check(self.work, self.stamp), 0)

    def test_untracked_files_do_not_block(self):
        (self.work / 'junk').write_text('x')
        self.assertEqual(update.check(self.work, self.stamp), 1)

    def test_offline_is_silent(self):
        git(self.work, 'remote', 'set-url', 'origin', str(self.tmp / 'missing.git'))
        self.assertEqual(update.check(self.work, self.stamp), 0)
        self.assertFalse(self.stamp.exists())


if __name__ == '__main__':
    unittest.main()
