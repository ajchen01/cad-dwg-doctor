from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from xref_graph import discover_graph

class XrefGraphTests(unittest.TestCase):
    def test_nested_shared_and_cyclic_dependencies_are_visited_once_children_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            a,b,c=[root/name for name in ['a.dwg','b.dwg','c.dwg']]
            edges={a:[b,c],b:[c],c:[a]}
            calls=[]
            def read(p):
                calls.append(p)
                return [{'name':q.stem,'path':str(q),'raw':q.name,'exists':True} for q in edges[p]]
            graph=discover_graph([a,b],read)
            self.assertEqual(set(graph['nodes']),{a,b,c})
            self.assertEqual(len(calls),3)
            self.assertLess(graph['nodes'].index(c),graph['nodes'].index(b))
            self.assertLess(graph['nodes'].index(b),graph['nodes'].index(a))
            self.assertEqual(graph['issues'],[])

    def test_missing_reference_reports_owner_and_fails_coverage(self):
        root=Path('/test').resolve()
        a=root/'a.dwg';missing=root/'missing.dwg'
        graph=discover_graph([a],lambda p:[{'name':'missing','path':str(missing),'raw':'missing.dwg','exists':False}])
        self.assertEqual(len(graph['issues']),1)
        self.assertEqual(graph['issues'][0]['owner'],str(a))
        self.assertFalse(graph['complete'])

    def test_depth_limit_reports_incomplete_instead_of_success(self):
        a=Path('a.dwg').resolve();b=Path('b.dwg').resolve();c=Path('c.dwg').resolve()
        def read(p):return [] if p==c else [{'name':'child','path':str(b if p==a else c),'exists':True}]
        graph=discover_graph([a],read,max_depth=1)
        self.assertFalse(graph['complete'])
        self.assertEqual(set(graph['nodes']),{a,b})

    def test_explicit_opt_out_reads_only_input(self):
        a=Path('a.dwg').resolve()
        calls=[]
        def read(p):calls.append(p);return []
        graph=discover_graph([a],read,with_xref=False)
        self.assertEqual(graph['nodes'],[a])
        self.assertEqual(calls,[])

    def test_read_failure_is_not_treated_as_no_references(self):
        a=Path('a.dwg').resolve()
        def read(p):raise ValueError('unreadable DWG')
        graph=discover_graph([a],read)
        self.assertFalse(graph['complete'])
        self.assertIn('unreadable',graph['issues'][0]['fatal'])

if __name__=='__main__':unittest.main()
