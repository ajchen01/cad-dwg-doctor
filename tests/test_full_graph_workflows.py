from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile,unittest,sys,hashlib
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import dwgpipe
from edu_workflow import run_edu_graph
from xref_graph import discover_graph

class FullGraphWorkflowTests(unittest.TestCase):
    def run_case(self,overwrite=False,fail_leaf=False,edit_leaf=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();out=root/'out'
            host,mid,leaf=[root/name for name in ['Host.dwg','Mid.dwg','Leaf.dwg']]
            for p in [host,mid,leaf]:p.write_bytes(b'AC1024original')
            edges={host:[mid],mid:[leaf],leaf:[]}
            graph=discover_graph([host],lambda p:[{'name':c.stem,'path':str(c),'raw':c.name,'exists':True} for c in edges[p]])
            calls=[]
            def strip(source,folder,timeout,**kwargs):
                calls.append(source)
                if fail_leaf and source==leaf:return {'input':str(source),'label':source.stem,'fatal':'leaf failed','size_before':1}
                candidate=folder/(source.stem+'-已去戳.dwg');candidate.write_bytes(b'AC1024clean')
                if edit_leaf and source==leaf:source.write_bytes(b'AC1024user edits')
                if source==host:self.assertIn(leaf,kwargs['reference_context']['effective'])
                return {'input':str(source),'label':source.stem,'output_dwg':str(candidate),'verify':'print','clean':True,'before':{'stamped':True},'after':{'stamped':False},'dwg_version_before':'AC1024','dwg_version_after':'AC1024','size_before':1}
            options=SimpleNamespace(timeout=10,no_xref=False,xref_depth=None,overwrite=overwrite,no_audit=False,verify='print',keep_temp=True)
            with patch.object(dwgpipe,'discover_operation_graph',return_value=graph),patch.object(dwgpipe,'edu_strip',side_effect=strip):
                rows=run_edu_graph([host],out,options,'dummy')
            return rows,[p.name for p in calls],{p.name:p.read_bytes() for p in [host,mid,leaf]},[p.read_bytes() for p in root.glob('*.bak')]

    def test_single_host_copy_processes_all_nested_references_first(self):
        rows,calls,contents,backups=self.run_case()
        self.assertEqual(calls,['Leaf.dwg','Mid.dwg','Host.dwg'])
        self.assertTrue(all(v==b'AC1024original' for v in contents.values()))
        self.assertEqual(backups,[])
        self.assertTrue(rows[-1].get('bundle'))

    def test_single_host_overwrite_backs_up_and_replaces_every_reference(self):
        rows,calls,contents,backups=self.run_case(overwrite=True)
        self.assertEqual(calls,['Leaf.dwg','Mid.dwg','Host.dwg'])
        self.assertEqual(len(backups),3)
        self.assertTrue(all(v==b'AC1024original' for v in backups))
        self.assertTrue(all(v==b'AC1024clean' for v in contents.values()))
        self.assertTrue(all(r.get('overwrite',{}).get('replaced') for r in rows))

    def test_failed_reference_blocks_host_processing_and_overwrite(self):
        rows,calls,contents,backups=self.run_case(overwrite=True,fail_leaf=True)
        self.assertEqual(calls,['Leaf.dwg'])
        self.assertTrue(all(r.get('fatal') for r in rows))
        self.assertEqual(backups,[])
        self.assertTrue(all(v==b'AC1024original' for v in contents.values()))

    def test_edit_during_processing_is_not_overwritten(self):
        rows,calls,contents,backups=self.run_case(overwrite=True,edit_leaf=True)
        self.assertEqual(contents['Leaf.dwg'],b'AC1024user edits')
        self.assertTrue(all(r.get('fatal') for r in rows))
        self.assertEqual(backups,[])

if __name__=='__main__':unittest.main()
