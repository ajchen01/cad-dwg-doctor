"""Clear EDU stamps across the full DWG reference graph, dependencies first."""
from pathlib import Path
import shutil
from safe_overwrite import sha256, overwrite_group


def run_edu_graph(targets, outdir, options, strip_acc):
    from dwgpipe import discover_operation_graph, edu_strip, build_bundle, _rel_to
    graph=discover_operation_graph(targets,options.timeout,not options.no_xref,options.xref_depth)
    outdir.mkdir(parents=True,exist_ok=True)
    if not graph['complete']:
        rows=[dict(issue,label=issue.get('name') or Path(issue.get('input') or issue['owner']).stem,
                   size_before=0) for issue in graph['issues']]
        rows.extend({'input':str(root),'label':root.stem,'size_before':root.stat().st_size,
                     'fatal':'参照发现不完整，未处理此图集'} for root in graph['roots'])
        return rows
    rows=[]
    effective={}
    failed=set()
    overwrite_cache={}
    for index,source in enumerate(graph['nodes'],1):
        label=f'{index:04d}-{source.stem}'
        dependencies=[Path(r['path']).resolve() for r in graph['edges'].get(source,[]) if r.get('path')]
        if any(path in failed for path in dependencies):
            row={'input':str(source),'label':label,'size_before':source.stat().st_size,
                 'fatal':'参照清戳或覆盖失败，未处理宿主图'}
        else:
            context={'source':source,'graph':graph,'effective':effective} if not options.no_xref else None
            try:
                source_hash=sha256(source)
                row=edu_strip(source,outdir,options.timeout,do_audit=not options.no_audit,
                              strip_acc=strip_acc,label=label,verify=options.verify,reference_context=context)
                row.setdefault('source_sha256',source_hash)
                if options.overwrite:
                    overwrite_group([row],overwrite_cache)
            except Exception as exc:
                row={'input':str(source),'label':label,'size_before':source.stat().st_size,
                     'fatal':f'{type(exc).__name__}: {exc}'}
        row['is_xref']=source not in graph['roots']
        rows.append(row)
        if row.get('fatal'):
            failed.add(source)
        else:
            effective[source]=Path(row.get('output_dwg') or source)
        if row.get('work') and not options.keep_temp and not row.get('fatal'):
            shutil.rmtree(row['work'],ignore_errors=True)
    # Preserve relative DWG paths in a clean deliverable bundle for copy-only workflows.
    if not failed and not options.overwrite and options.verify=='print':
        by_path={Path(row['input']).resolve():row for row in rows}
        for index,root in enumerate(graph['roots'],1):
            if not graph['edges'].get(root):
                continue
            reachable=set();pending=[root]
            while pending:
                parent=pending.pop()
                for ref in graph['edges'].get(parent,[]):
                    child=Path(ref['path']).resolve()
                    if child!=root and child in effective and child not in reachable:
                        reachable.add(child);pending.append(child)
            refs=[{'input':str(p),'output_dwg':str(effective[p]),'rel':_rel_to(p,root.parent)} for p in reachable]
            absolute=any(Path(ref.get('raw','')).is_absolute()
                         for parent in reachable|{root} for ref in graph['edges'].get(parent,[]))
            if absolute or any(not ref['rel'] for ref in refs):
                by_path[root]['bundle_warning']='参照含绝对路径或位于主图目录外，未生成可移植整包'
                continue
            folder=outdir/f'整包-{index:04d}-{root.stem}'
            by_path[root]['bundle']=build_bundle(folder,root,{'output_dwg':str(effective[root])},refs)
    return rows
