import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from safe_overwrite import replace_with_backup, overwrite_result

class OverwriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'drawing.dwg'
        self.candidate = self.root / 'fixed.dwg'
        self.original.write_bytes(b'AC1024original')
        self.candidate.write_bytes(b'AC1024repaired')
        self.expected = hashlib.sha256(self.original.read_bytes()).hexdigest()

    def replace(self):
        return replace_with_backup(self.original, self.candidate, self.expected)

    def test_verified_backup_and_replacement(self):
        result = self.replace()
        self.assertEqual(Path(result['backup_path']).read_bytes(), b'AC1024original')
        self.assertEqual(result['backup_sha256'], self.expected)
        self.assertEqual(self.original.read_bytes(), b'AC1024repaired')
        self.assertEqual(self.candidate.read_bytes(), b'AC1024repaired')

    def test_backup_failure_preserves_original(self):
        with patch('safe_overwrite.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

    def test_corrupt_backup_preserves_original(self):
        def corrupt(src, dst):
            Path(dst).write_bytes(b'bad backup')
        with patch('safe_overwrite.shutil.copy2', side_effect=corrupt):
            with self.assertRaises(ValueError):
                self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

    def test_changed_input_preserves_user_edits(self):
        self.original.write_bytes(b'AC1024new user edits')
        with self.assertRaises(ValueError):
            self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024new user edits')

    def test_version_mismatch_preserves_original(self):
        self.candidate.write_bytes(b'AC1032upgraded')
        with self.assertRaises(ValueError):
            self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

    def test_atomic_replace_failure_keeps_original_and_backup(self):
        with patch('safe_overwrite.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024original')
        backups=list(self.root.glob('*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b'AC1024original')

    def test_repeated_replacement_keeps_distinct_backups(self):
        first=self.replace()
        expected=hashlib.sha256(self.original.read_bytes()).hexdigest()
        second=replace_with_backup(self.original, self.candidate, expected)
        self.assertNotEqual(first['backup_path'], second['backup_path'])
        self.assertEqual(Path(first['backup_path']).read_bytes(), b'AC1024original')

    def test_unverified_strip_cannot_overwrite(self):
        result={'input':str(self.original),'output_dwg':str(self.candidate),'verify':'none','clean':False}
        overwrite_result(result,self.expected)
        self.assertIn('fatal',result)
        self.assertNotIn('overwrite',result)
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

    def test_failed_result_cannot_overwrite(self):
        result={'input':str(self.original),'output_dwg':str(self.candidate),'fatal':'verification failed'}
        overwrite_result(result,self.expected)
        self.assertNotIn('overwrite',result)
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

    def test_cad_lock_blocks_replacement(self):
        self.original.with_suffix('.dwl2').touch()
        with self.assertRaises(ValueError):
            self.replace()
        self.assertEqual(self.original.read_bytes(), b'AC1024original')

if __name__=='__main__':
    unittest.main()
