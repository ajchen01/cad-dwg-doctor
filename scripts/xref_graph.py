"""DWG dependency traversal and isolated reference context for print verification."""
from __future__ import annotations
from pathlib import Path
import shutil


def discover_graph(roots, read_refs, *, with_xref=True, max_depth=None):
    roots=list(dict.fromkeys(Path(p).resolve() for p in roots))
    edges, nodes, issues, active, done = {}, [], [], set(), set()
    for root in roots:
        stack=[(root,0,False)]
        while stack:
            path,depth,exit_node=stack.pop()
            if exit_node:
                active.discard(path)
                if path not in done:
                    done.add(path);nodes.append(path)
                continue
            if path in done or path in active:
                continue
            active.add(path)
            stack.append((path,depth,True))
            try:
                refs=read_refs(path) if with_xref else []
                edges[path]=refs
            except Exception as exc:
                issues.append({'input':str(path),'owner':str(path),'fatal':f'参照发现失败：{exc}'})
                edges[path]=[]
                continue
            for ref in reversed(refs):
                child=Path(ref['path']).resolve() if ref.get('path') else None
                if child is None or not ref.get('exists'):
                    issues.append({'input':str(child) if child else None,'owner':str(path),'name':ref.get('name'),'fatal':'参照文件缺失或路径为空'})
                elif child.suffix.lower()=='.dwg':
                    if max_depth is not None and depth>=max_depth:
                        issues.append({'input':str(child),'owner':str(path),'fatal':'达到参照深度限制，未完整处理','depth_limit':True})
                    elif child not in active and child not in done:
                        stack.append((child,depth+1,False))
    issues=[r for r in issues if not (r.get('depth_limit') and Path(r['input']) in done)]
    return {'roots':roots,'nodes':nodes,'edges':edges,'issues':issues,'complete':not issues}


def _quoted(value):
    return '"'+str(value).replace('\\','/').replace('"','\\"')+'"'


def repath_commands(refs, snapshots):
    commands=[]
    for ref in refs:
        if not ref.get('path'):
            continue
        target=snapshots.get(Path(ref['path']).resolve())
        if target is None:
            continue
        if ref.get('handle'):
            name=f'(cdr (assoc 2 (entget (handent {_quoted(ref["handle"])}))))'
        else:
            name=_quoted(ref['name'])
        commands.append(f'(vl-cmdf "_.-XREF" "_PATH" {name} {_quoted(target.as_posix())})')
        entity=f'(handent {_quoted(ref["handle"])})' if ref.get('handle') else f'(tblobjname "BLOCK" {name})'
        commands.append(f'(setq xrpath (cdr (assoc 1 (entget {entity}))))')
        commands.append('(if (or (not xrpath) (/= (strcase (vl-string-translate "\\\\" "/" xrpath)) '
                        + f'(strcase {_quoted(target.as_posix())}))) (princ "\\n[MARK-XREF-PATH-FAILED]"))')
    if commands:
        commands.append('(vl-cmdf "_.-XREF" "_RELOAD" "*")')
    return '\n'.join(commands)+'\n' if commands else ''


def print_snapshot_context(acc, source, graph, effective, work, timeout, run_session, save_formats):
    """Redirect only temporary print copies to processed reference outputs."""
    source=Path(source).resolve()
    reachable=set()
    pending=[source]
    while pending:
        parent=pending.pop()
        for ref in graph['edges'].get(parent,[]):
            if not ref.get('path') or not ref.get('exists'):
                continue
            child=Path(ref['path']).resolve()
            if child in graph['edges'] and child not in reachable:
                reachable.add(child);pending.append(child)
    if not reachable:
        return ''
    room=work/'refs'
    room.mkdir(exist_ok=True)
    snapshots={path:room/f'ref{index:04d}.dwg' for index,path in enumerate(sorted(reachable),1)}
    # Materialize every file before repathing, so shared/cyclic references have a target.
    for original,target in snapshots.items():
        shutil.copy2(effective.get(original,original),target)
    for index,original in enumerate(p for p in graph['nodes'] if p in reachable):
        commands=repath_commands(graph['edges'].get(original,[]),snapshots)
        if not commands:
            continue
        target=snapshots[original]
        saved=room/f'saved{index:04d}.dwg'
        with target.open('rb') as stream:
            version=stream.read(6)
        save_format=save_formats.get(version)
        if save_format is None:
            raise ValueError(f'参照打印快照不能保留原格式：{original}')
        # The snapshots are disposable printing context, never delivered or used as backups.
        script=room/f'paths{index:04d}.scr'
        script.write_text('(setvar "FILEDIA" 0)\n(setvar "CMDDIA" 0)\n'+commands+
                          f'(vl-cmdf "_.SAVEAS" "{save_format}" {_quoted(saved.as_posix())})\n(princ "\\n[MARK-XREF-SNAPSHOT]")\n',encoding='ascii')
        rc,log=run_session(acc,target,script,timeout,room/f'paths{index:04d}.log')
        if rc or '[MARK-XREF-SNAPSHOT]' not in log or '\n[MARK-XREF-PATH-FAILED]' in log.replace('\r', '') or not saved.is_file():
            raise ValueError(f'参照打印快照失败：{original}')
        with saved.open('rb') as stream:
            if stream.read(6)!=version:
                raise ValueError(f'参照打印快照版本变化：{original}')
        shutil.copy2(saved,target)
    return repath_commands(graph['edges'].get(source,[]),snapshots)
