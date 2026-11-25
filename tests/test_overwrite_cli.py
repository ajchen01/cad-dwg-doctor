import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dwgpipe

class OverwriteCliTests(unittest.TestCase):
    def run_case(self, overwrite=False, failure=False, include_ref=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'input.dwg'
            source.write_bytes(b'AC1024original')
            ref=root/'ref.dwg'
            ref.write_bytes(b'AC1024ref-original')
            out=root/'out'
            def process(*args, **kwargs):
                out.mkdir(exist_ok=True)
                candidate=out/'input-已修复.dwg'
                candidate.write_bytes(b'AC1024repaired')
                refs=[]
                if include_ref:
                    fixed_ref=out/'ref-fixed.dwg';fixed_ref.write_bytes(b'AC1024ref-repaired')
                    refs=[{'input':str(ref),'output_dwg':str(fixed_ref),'after':{'verified':True},'dwg_version_before':'AC1024','dwg_version_after':'AC1024','source_sha256':hashlib.sha256(b'AC1024ref-original').hexdigest()}]
                return {'main': {'input':str(source),'output_dwg':str(candidate),'before':{},'after':{'verified':True},'dwg_version_before':'AC1024','dwg_version_after':'AC1024'},'refs':refs}
            argv=['dwgpipe.py','all',str(source),'--out',str(out),'--keep-temp']
            if overwrite:argv.append('--overwrite')
            with patch.object(sys,'argv',argv), patch.object(dwgpipe,'process_with_xrefs',side_effect=process), patch.object(dwgpipe,'print_summary'), patch.object(dwgpipe,'write_xref_table'), contextlib.redirect_stdout(io.StringIO()):
                if failure:
                    with patch('safe_overwrite.shutil.copy2',side_effect=OSError('backup denied')):
                        with self.assertRaises(SystemExit):dwgpipe.main()
                else:dwgpipe.main()
            report=json.loads((out/'input-report.json').read_text(encoding='utf-8'))
            return source.read_bytes(), [p.read_bytes() for p in root.glob('*.bak')], report

    def test_default_keeps_original_without_backups(self):
        source,backups,report=self.run_case()
        self.assertEqual(source,b'AC1024original')
        self.assertEqual(backups,[])
        self.assertNotIn('overwrite',report['main'])

    def test_explicit_flag_creates_verified_backup_and_records_it(self):
        source,backups,report=self.run_case(overwrite=True)
        self.assertEqual(source,b'AC1024repaired')
        self.assertEqual(backups,[b'AC1024original'])
        self.assertEqual(report['main']['overwrite']['backup_sha256'],hashlib.sha256(b'AC1024original').hexdigest())

    def test_overwrite_includes_followed_reference_with_its_own_backup(self):
        source,backups,report=self.run_case(overwrite=True,include_ref=True)
        self.assertEqual(source,b'AC1024repaired')
        self.assertEqual(set(backups),{b'AC1024original',b'AC1024ref-original'})
        self.assertTrue(report['refs'][0]['overwrite']['replaced'])

    def test_backup_failure_returns_nonzero_and_keeps_original(self):
        source,backups,report=self.run_case(overwrite=True,failure=True)
        self.assertEqual(source,b'AC1024original')
        self.assertIn('fatal',report['main'])

    def test_readonly_and_unverified_modes_reject_overwrite(self):
        for args in [('audit',),('educheck',),('edustrip','--verify','none')]:
            with self.subTest(args=args), patch.object(sys,'argv',['dwgpipe.py',args[0],'does-not-exist.dwg',*args[1:],'--overwrite']), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:dwgpipe.main()
                self.assertEqual(error.exception.code,2)

if __name__=='__main__':
    unittest.main()
