import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import dwgpipe
import eduprint
import batch_check

class RegressionTests(unittest.TestCase):
    def test_plot_timeout_preserves_diagnostic_output(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failure = subprocess.TimeoutExpired('accoreconsole', 1,
                output='[MARK-PLOT-DONE] waiting for confirmation'.encode('utf-16-le'))
            with patch.object(eduprint.subprocess, 'run', side_effect=failure):
                code, log = eduprint._plot('dummy', root / 'a.dwg', root, root / 'a.pdf', 1)
            self.assertEqual(code, 124)
            self.assertIn('[MARK-PLOT-DONE]', log)
            self.assertIn('waiting for confirmation', log)

    def test_print_collection_excludes_attach_and_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.dwg').touch()
            (root / 'a-已修复.dwg').touch()
            (root / 'ref.dwg.Attach').mkdir()
            (root / 'ref.dwg.Attach' / 'b.dwg').touch()
            self.assertEqual(eduprint.collect([tmp]), [root / 'a.dwg'])

    def test_batch_collection_excludes_directories_named_dwg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.dwg').mkdir()
            self.assertEqual(batch_check.collect(root, False), [])

    def test_precheck_failure_never_runs_strip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'a.dwg'
            src.write_bytes(b'AC1032original')
            with patch.object(dwgpipe, '_new_workdir', return_value=root), patch.object(dwgpipe, '_print_judge', return_value={'stamped': None, 'fatal': 'plot failed'}), patch.object(dwgpipe, 'run_session') as run:
                r = dwgpipe.edu_strip(src, root / 'out', 10, strip_acc='dummy')
            self.assertIn('fatal', r)
            run.assert_not_called()
            self.assertNotIn('output_dwg', r)
            self.assertEqual(src.read_bytes(), b'AC1032original')

    def test_failed_save_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'a.dwg'
            src.write_bytes(b'AC1032original')
            with patch.object(dwgpipe, '_new_workdir', return_value=root), patch.object(dwgpipe, 'run_session', return_value=(1, '')):
                r = dwgpipe.edu_strip(src, root / 'out', 10, strip_acc='dummy', verify='none')
            self.assertIn('fatal', r)
            self.assertNotIn('output_dwg', r)
            self.assertFalse((root / 'out' / 'a-已去戳.dwg').exists())

    def test_failed_postcheck_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'a.dwg'
            src.write_bytes(b'AC1032original')
            with patch.object(dwgpipe, '_new_workdir', return_value=root), patch.object(dwgpipe, '_print_judge', side_effect=[{'stamped': True}, {'stamped': None, 'fatal': 'bad pdf'}]), patch.object(dwgpipe, 'run_session', return_value=(0, '[MARK-QSAVE]')):
                r = dwgpipe.edu_strip(src, root / 'out', 10, strip_acc='dummy')
            self.assertIn('fatal', r)
            self.assertNotIn('output_dwg', r)

    def test_nonzero_plot_cannot_be_classified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'a.dwg'
            src.write_bytes(b'AC1032original')
            with patch.object(eduprint, '_plot', return_value=(1, 'error')), patch.object(eduprint, '_edge_stats') as analyze:
                r = eduprint.check_one('dummy', src, root / 'out', 150, 10)
            self.assertIn('fatal', r)
            analyze.assert_not_called()

class BundleTests(unittest.TestCase):
    def run_case(self, raw, nested=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / 'host.dwg'
            ref = root / 'ref.dwg'
            child = root / 'child.dwg'
            main = {'xrefs': [{'path': str(ref), 'raw': str(ref) if raw == 'absolute' else 'ref.dwg', 'exists': True}], 'output_dwg': str(host)}
            target = {'input': str(ref), 'output_dwg': str(ref), 'xrefs': [{'path': str(child), 'raw': 'child.dwg', 'exists': True}] if nested else []}
            with patch.object(dwgpipe, 'pipeline', side_effect=[main, target]), patch.object(dwgpipe, 'write_xref_table'), patch.object(dwgpipe, 'build_bundle', return_value='bundle') as build:
                result = dwgpipe.process_with_xrefs(host, root / 'out', True, True, 10)
            return result, build.called

    def test_absolute_reference_does_not_create_portable_bundle(self):
        result, built = self.run_case('absolute')
        self.assertFalse(built)
        self.assertIn('bundle_warning', result)

    def test_depth_limit_does_not_create_incomplete_bundle(self):
        result, built = self.run_case('relative', nested=True)
        self.assertFalse(built)
        self.assertIn('bundle_warning', result)

    def test_complete_relative_reference_can_be_bundled(self):
        result, built = self.run_case('relative')
        self.assertTrue(built)
        self.assertEqual(result['bundle'], 'bundle')

class ArtifactTests(unittest.TestCase):
    def test_package_contains_required_resources_and_no_drawings(self):
        from package_skill import build
        from zipfile import ZipFile
        with tempfile.TemporaryDirectory() as tmp:
            path = build(Path(tmp) / 'skill.zip')
            with ZipFile(path) as archive:
                names = archive.namelist()
                self.assertIn('cad-dwg-doctor/SKILL.md', names)
                self.assertIn('cad-dwg-doctor/scripts/dwgpipe.py', names)
                self.assertIn('cad-dwg-doctor/references/operations.md', names)
                self.assertFalse(any(n.lower().endswith(('.dwg', '.dxf', '.pdf')) for n in names))

    def test_auditor_reads_model_and_paper_space(self):
        import ezdxf
        from audit_rules import Auditor
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.dxf'
            doc = ezdxf.new('R2013')
            doc.modelspace().add_line((0, 0), (0, 0))
            doc.layouts.get('Layout1').add_text('EDUCATION drawing note')
            doc.saveas(path)
            before = path.read_bytes()
            audit = Auditor(str(path)).run()
            counts = audit.entity_counts()
            self.assertEqual(counts['Model']['LINE'], 1)
            self.assertEqual(counts['Layout1']['TEXT'], 1)
            self.assertFalse(any(f.code in ('R00', 'F04') for f in audit.findings))
            self.assertIsNone(audit.stats['edu'])
            self.assertEqual(path.read_bytes(), before)

if __name__ == '__main__':
    unittest.main()
