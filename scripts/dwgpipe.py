# -*- coding: utf-8 -*-
"""DWG 修复 / 核查 / 清理 一条龙。

三条硬原则
1. 绝不在原文件上动手：先复制到纯 ASCII 临时目录，改副本。
2. 修复与清理只用 CAD 原生命令（AUDIT / PURGE / OVERKILL / entdel），
   不把 DXF 写回 DWG —— DXF 往返会把天正、探索者等自定义对象降级成代理块。
3. 核查读的是 DXFOUT 出来的副本，只读，不动图。

外部参照默认跟随处理：主图带 xref 时，被参照的 DWG 走同一套流程，
修好的主图与参照按原相对路径拼成 整包-已修复/（不给整包，参照路径必断）。

用法
    python dwgpipe.py audit  <dwg> [--out DIR] [--no-xref]
    python dwgpipe.py repair <dwg> [--out DIR] [--clean]
    python dwgpipe.py all    <dwg> [--out DIR] [--xref-depth 2]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
def _rules():
    """audit_rules 依赖 ezdxf；educheck/deedu 用不到，做成懒加载。"""
    from audit_rules import Auditor, markdown_report
    return Auditor, markdown_report

# AutoCAD 常见安装位置。除了系统盘，还会被装到别的盘（自定义安装很常见）。
AUTODESK_ROOTS = [
    Path(r"C:\Program Files\Autodesk"),
    Path(r"D:\Program Files\Autodesk"),
    Path(r"E:\Program Files\Autodesk"),
]

# 装在非标准位置（绿色版、便携版）时，用这个环境变量直接指到 accoreconsole.exe
ENV_ACC = "ACCORECONSOLE"

# 命令行 --acc / --strip-acc 的落点。main() 里赋值一次，find_acc() 自己读，
# 这样不用把参数一路穿过十几个函数签名。
_ACC_OVERRIDE: str | None = None
_STRIP_ACC_OVERRIDE: str | None = None

# 清教育版戳的最低版本。官方口径：2019-2021 是"弹框 + 洗不掉"的三年，
# 只有 2022 及更高打开另存能清。
EDU_STRIP_MIN_YEAR = 2022

# ---------------- scr 模板（AutoLISP） ----------------

PRELUDE = r"""(setvar "FILEDIA" 0)
(setvar "CMDDIA" 0)
(setvar "EXPERT" 5)
(setvar "AUDITCTL" 1)
"""


def _scr_audit_and_purge(purge_rounds: int) -> str:
    s = [PRELUDE, r'(vl-cmdf "_.AUDIT" "_Y")', r'(princ "\n[MARK-AUDIT]")']
    for _ in range(purge_rounds):
        for code in ("B", "LA", "LT", "ST", "D", "M", "R", "G"):
            s.append(f'(vl-cmdf "_.-PURGE" "_{code}" "*" "_N")')
        s.append(r'(princ "\n[MARK-PURGE]")')
    return "\n".join(s) + "\n"


# OVERKILL 实测会让无头会话在退出时挂死（2020，60s 超时未返回，且 QSAVE 无法执行），已停用。
# 重复实体改为只报告，由人工在 GUI 里跑 OVERKILL。

SCR_CLEAN = r"""
;; delete zero-length lines
(setq ss (ssget "_X" '((0 . "LINE"))) i 0 n 0)
(if ss
  (repeat (sslength ss)
    (setq en (ssname ss i) d (entget en) i (1+ i))
    (if (< (distance (cdr (assoc 10 d)) (cdr (assoc 11 d))) 1e-6)
      (progn (entdel en) (setq n (1+ n))))))
(princ (strcat "\n[MARK-CLEAN-ZEROLEN] " (itoa n)))

;; delete empty TEXT
(setq ss (ssget "_X" '((0 . "TEXT"))) i 0 m 0)
(if ss
  (repeat (sslength ss)
    (setq en (ssname ss i) d (entget en) i (1+ i))
    (if (= (vl-string-trim " " (cdr (assoc 1 d))) "")
      (progn (entdel en) (setq m (1+ m))))))
(princ (strcat "\n[MARK-CLEAN-EMPTYTEXT] " (itoa m)))

;; delete empty MTEXT (blank, or only line-break codes \P)
;; Only the provably-invisible ones: content is empty after removing \P.
;; MTEXT carrying only format codes is NOT deleted here -- stripping format
;; codes in LISP is easy to get wrong; the report flags it for a human instead.
(setq ss (ssget "_X" '((0 . "MTEXT"))) i 0 k 0)
(if ss
  (repeat (sslength ss)
    (setq en (ssname ss i) d (entget en) i (1+ i))
    (setq s (cdr (assoc 1 d)))
    (if (and s (not (assoc 3 d)))
      (progn
        (while (vl-string-search "\\P" s) (setq s (vl-string-subst "" "\\P" s)))
        (if (= (vl-string-trim " \t" s) "")
          (progn (entdel en) (setq k (1+ k))))))))
(princ (strcat "\n[MARK-CLEAN-EMPTYMTEXT] " (itoa k)))
"""


def dxfout(path_posix: str, tag: str) -> str:
    return f'(vl-cmdf "_.DXFOUT" "{path_posix}" "16")\n(princ "\\n[MARK-DXF-{tag}]")\n'


_YEAR_RE = re.compile(r"AutoCAD\s+(\d{4})")


def discover_acc() -> list:
    """扫出本机所有可用的 accoreconsole，返回 [(年份, 路径)]，年份降序。

    三个来源：
      1. 环境变量 `ACCORECONSOLE` —— 精确指定，装在非常规位置时用
      2. `AUTODESK_ROOTS` 下的 `AutoCAD 20xx` 目录
      3. PATH 里的 `accoreconsole`
    年份从目录名取；取不到记 0（排序时垫底，不会被当成"最老"）。"""
    found: list = []

    def _add(year: int, p):
        p = str(Path(p))
        if os.path.isfile(p) and all(p != q for _, q in found):
            found.append((year, p))

    env = os.environ.get(ENV_ACC, "").strip().strip('"')
    if env:
        m = _YEAR_RE.search(env)
        _add(int(m.group(1)) if m else 0, env)

    for root in AUTODESK_ROOTS:
        try:
            for d in root.iterdir():
                m = _YEAR_RE.fullmatch(d.name)
                if m:
                    _add(int(m.group(1)), d / "accoreconsole.exe")
        except OSError:
            continue

    which = shutil.which("accoreconsole")
    if which:
        m = _YEAR_RE.search(which)
        _add(int(m.group(1)) if m else 0, which)

    return sorted(found, key=lambda x: x[0], reverse=True)


def find_acc(min_year: int | None = None, acc: str | None = None) -> str:
    """挑一个 accoreconsole。

    `acc`：显式指定路径，优先级最高（对应 `--acc` 参数）。
    `min_year`：只在该年份及以上挑最高的。清教育版戳必须走这条，
      因为 2019-2021 实测洗不掉，只有 2022+ 另存能清。
    都不给：挑**最老**的那个。判定与打印基准在老内核上校准过；版本越新越可能
      带教育版授权，而教育版内核打印会给每张图盖戳，全目录会被判成「带戳」。
      所以默认往下挑，不往上挑。"""
    acc = acc or (_STRIP_ACC_OVERRIDE if min_year else _ACC_OVERRIDE)
    if acc:
        if not os.path.isfile(acc):
            raise SystemExit(f"指定的 accoreconsole 不存在：{acc}")
        return acc

    cands = discover_acc()
    if min_year:
        ok = [(y, p) for y, p in cands if y >= min_year]
        if not ok:
            have = "、".join(str(y or "版本未知") for y, _ in cands) or "无"
            raise SystemExit(
                f"没找到 AutoCAD {min_year} 或更高版本的无头引擎（accoreconsole.exe）。\n"
                f"已检出：{have}\n"
                f"清教育版戳必须用 {min_year}+ 另存一次，老版本做不到（2019-2021 实测无效）。\n"
                f"装在非标准位置时，设环境变量 {ENV_ACC}，或用 --acc 指定路径。")
        return ok[0][1]

    if not cands:
        raise SystemExit(
            "找不到 accoreconsole.exe。\n"
            f"  ① 装 AutoCAD 2020 或更高（任一版本都带这个无头引擎，不需要额外组件）\n"
            f"  ② 装在非标准位置，设环境变量 {ENV_ACC} 指向它\n"
            f"  ③ 或用 --acc 显式指定")
    # 年月未知(0) 的排最后；其余取年份最小的
    return min(cands, key=lambda x: (x[0] == 0, x[0]))[1]


def run_session(acc: str, dwg: Path, scr: Path, timeout: int, log: Path) -> tuple[int, str]:
    """跑一次无头会话。scr 与 dwg 必须是纯 ASCII 路径。"""
    for p in (str(dwg), str(scr)):
        try:
            p.encode("ascii")
        except UnicodeEncodeError:
            raise SystemExit(f"路径含非 ASCII 字符，accoreconsole 会解析失败：{p}")
    t0 = time.time()
    proc = subprocess.run([acc, "/i", str(dwg), "/s", str(scr)],
                          capture_output=True, timeout=timeout)
    out = proc.stdout.decode("utf-16-le", errors="ignore") if proc.stdout else ""
    out += proc.stderr.decode("utf-16-le", errors="ignore") if proc.stderr else ""
    log.write_text(out, encoding="utf-8", errors="ignore")
    return proc.returncode, out


def parse_adt(adt: Path) -> dict:
    """AUDIT 报告是 GBK 文本，抠出「检查到 N 个错误 / 已修复 N 个 / 已删除 N 个对象」"""
    res = {"raw": "", "errors": None, "fixed": None, "deleted": None}
    if not adt.exists():
        return res
    txt = adt.read_bytes().decode("gbk", errors="replace")
    res["raw"] = txt
    m = re.search(r"(\d+)\s*个错误[，,]\s*已修复\s*(\d+)\s*个", txt)
    if m:
        res["errors"], res["fixed"] = int(m.group(1)), int(m.group(2))
    m2 = re.search(r"已删除\s*(\d+)\s*个对象", txt)
    if m2:
        res["deleted"] = int(m2.group(1))
    return res


def summarize_dxf(dxf: Path) -> dict:
    Auditor, markdown_report = _rules()
    a = Auditor(str(dxf)).run()
    return {
        "stats": a.stats,
        "counts": a.counts,
        "findings": [f.to_dict() for f in a.findings],
        "entities": a.entity_counts(),
        "report_md": markdown_report(a, dxf.stem),
    }


_WORK_SEQ = [0]


def _new_workdir(prefix: str = "") -> Path:
    """每次会话一个独立临时目录。批量/参照连跑时同秒同 pid 会撞名，加序号。"""
    _WORK_SEQ[0] += 1
    name = f"{prefix}{time.strftime('%H%M%S')}_{os.getpid()}_{_WORK_SEQ[0]}"
    work = Path(tempfile.gettempdir()) / "dwgfix" / name
    work.mkdir(parents=True, exist_ok=True)
    return work


def _stage_room(work: Path, src_dwg: Path) -> list[str]:
    """让参照在「解析得到」的状态下被打开，AUDIT / PURGE 才是真实环境下的结果。

    两种情况：
    - 主图：把同目录的 *.dwg.Attach 参照目录整套复刻过去；
    - 参照图（它自己也可能带参照）：同目录没有 .Attach 时，把同目录的其它 DWG 平铺复制过去
      （嵌套参照一般是同级文件名引用）。
    返回做过的事，便于日志核对。"""
    notes = []
    try:
        attaches = [d for d in src_dwg.parent.iterdir()
                    if d.is_dir() and d.name.lower().endswith(".dwg.attach")]
        if attaches:
            for d in attaches:
                dst = work / d.name
                if not dst.exists():
                    shutil.copytree(d, dst, ignore=shutil.ignore_patterns("*.dwl", "*.dwl2"))
            notes.append(f"{len(attaches)} 个参照目录")
        else:
            n = 0
            for f in src_dwg.parent.iterdir():
                if f.is_file() and f.suffix.lower() == ".dwg" \
                        and f.name.lower() != "in.dwg" and f.resolve() != src_dwg.resolve():
                    shutil.copy2(f, work / f.name)
                    n += 1
            if n:
                notes.append(f"{n} 个同级 DWG")
    except OSError:
        pass
    return notes


MARK_CLEAN_RE = re.compile(r"\[MARK-CLEAN-([A-Z]+)\]\s*(\d+)")


def parse_clean(txt: str) -> dict:
    """抠出本次删了多少零长线 / 空 TEXT / 空 MTEXT"""
    return {k.lower(): int(v) for k, v in MARK_CLEAN_RE.findall(txt or "")}


def pipeline(dwg: Path, outdir: Path, do_repair: bool, do_clean: bool, timeout: int,
             purge_rounds: int = 3, label: str | None = None,
             stage_room: bool = False) -> dict:
    label = label or dwg.stem
    acc = find_acc()
    work = _new_workdir()
    src = work / "in.dwg"
    shutil.copy2(dwg, src)
    outdir.mkdir(parents=True, exist_ok=True)
    result = {"input": str(dwg), "label": label, "work": str(work), "accoreconsole": acc}
    if stage_room:
        rooms = _stage_room(work, dwg)
        if rooms:
            result["staged_rooms"] = rooms

    # ---- 1) 只读体检（DXFOUT 到 before.dxf，随后才动图）----
    scr_parts = [PRELUDE, dxfout((work / "before.dxf").as_posix(), "BEFORE")]
    if do_repair:
        scr_parts.append(_scr_audit_and_purge(purge_rounds))
        if do_clean:
            scr_parts.append(SCR_CLEAN)
        scr_parts.append(r'(vl-cmdf "_.QSAVE")' + "\n" + r'(princ "\n[MARK-QSAVE]")' + "\n")
        scr_parts.append(dxfout((work / "after.dxf").as_posix(), "AFTER"))
    scr_parts.append(r'(princ "\n[MARK-END]")' + "\n")
    scr = work / "run.scr"
    scr.write_text("".join(scr_parts), encoding="ascii")

    rc, out = run_session(acc, src, scr, timeout, work / "session.log")
    result["rc"] = rc
    result["unresolved_xref"] = out.count("未找到")

    before_dxf, after_dxf = work / "before.dxf", work / "after.dxf"
    if not before_dxf.exists():
        result["fatal"] = ("导出 DXF 失败：文件可能损坏或版本过高。"
                           "先试 RECOVER 打开，或确认 accoreconsole 能读该 DWG 版本。")
        return result

    result["before"] = summarize_dxf(before_dxf)
    (outdir / f"{label}-体检报告-修复前.md").write_text(result["before"]["report_md"], encoding="utf-8")
    result["xrefs"] = extract_xrefs(before_dxf, dwg.parent)

    if do_repair:
        adt = work / "in.adt"
        result["audit_log"] = parse_adt(adt)
        result["cleaned"] = parse_clean(out)
        if after_dxf.exists():
            result["after"] = summarize_dxf(after_dxf)
            (outdir / f"{label}-体检报告-修复后.md").write_text(result["after"]["report_md"], encoding="utf-8")
        fixed = outdir / f"{label}-已修复.dwg"
        shutil.copy2(src, fixed)
        result["output_dwg"] = str(fixed)
        result["size_before"] = dwg.stat().st_size
        result["size_after"] = fixed.stat().st_size
    else:
        dxf_copy = outdir / f"{label}-体检副本.dxf"
        shutil.copy2(before_dxf, dxf_copy)
        result["output_dxf"] = str(dxf_copy)

    return result


def extract_xrefs(dxf: Path, base_dir: Path) -> list[dict]:
    """从 DXF 块定义里读外部参照路径（组码 1 = xref_path），解析成本机实际文件。
    相对路径按主图所在目录解析；找不到的也列出来——"参照丢了"本身就是要报的问题。"""
    out = []
    try:
        import ezdxf
        doc = ezdxf.readfile(str(dxf))
    except Exception:
        return out
    for b in doc.blocks:
        try:
            if not (b.block.dxf.flags & 4):
                continue
            raw = str(b.block.dxf.get("xref_path", "") or "")
        except Exception:
            continue
        if not raw:
            out.append({"name": b.name, "raw": "", "path": None, "exists": False})
            continue
        p = Path(raw.replace("\\", os.sep))
        if not p.is_absolute():
            p = base_dir / p
        p = p.resolve()
        out.append({"name": b.name, "raw": raw, "path": str(p), "exists": p.is_file()})
    return out


def _attach_tag(p: Path) -> str:
    """从路径里取 *.dwg.Attach 目录名（去掉后缀那一段），用来给重名参照做前缀"""
    for part in p.parts:
        low = part.lower()
        if low.endswith(".dwg.attach"):
            return part[:len(part) - len(".dwg.Attach")]
    return ""


def _rel_to(p: Path, root: Path) -> str | None:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def process_with_xrefs(dwg: Path, outdir: Path, do_repair: bool, do_clean: bool,
                       timeout: int, purge_rounds: int = 3, with_xref: bool = True,
                       max_depth: int = 1, seen: set | None = None) -> dict:
    """主图 + 外部参照一起处理。

    参照走的是同一套流程（体检 / AUDIT / PURGE / 清理），报告落在 outdir/外部参照/，
    修好的参照按原来的相对路径摆进 outdir/整包-已修复/，这样主图里的参照还能解析。
    原图一律不动。
    """
    root = dwg.parent.resolve()
    seen = set() if seen is None else seen
    xout = outdir / "外部参照"
    refs: list[dict] = []

    main_r = pipeline(dwg, outdir, do_repair, do_clean, timeout, purge_rounds,
                      label=dwg.stem, stage_room=with_xref)
    seen.add(dwg.resolve())
    result = {"main": main_r, "refs": refs, "root": str(root), "bundle": None}
    if main_r.get("fatal") or not with_xref:
        return result

    frontier = [(main_r, 0)]
    used_labels = {dwg.stem}
    while frontier:
        parent_r, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        for x in parent_r.get("xrefs") or []:
            if not x.get("path"):
                refs.append({"name": x.get("name"), "input": None, "fatal": "参照路径为空（未保存路径）"})
                continue
            pp = Path(x["path"]).resolve()
            if not x.get("exists"):
                refs.append({"name": x["name"], "input": str(pp), "fatal": "参照文件缺失"})
                continue
            if pp in seen or pp.suffix.lower() != ".dwg":
                continue
            if any(part.lower().endswith(".dwl") for part in pp.parts):
                continue
            seen.add(pp)
            label = pp.stem
            if label in used_labels:
                label = f"{_attach_tag(pp) or pp.parent.name}_{label}"
            used_labels.add(label)
            print(f"   └ 参照：{pp.name}", flush=True)
            r = pipeline(pp, xout, do_repair, do_clean, timeout, purge_rounds,
                         label=label, stage_room=True)
            r["rel"] = _rel_to(pp, root)
            r["is_xref"] = True
            refs.append(r)
            if not r.get("fatal"):
                frontier.append((r, depth + 1))

    if do_repair:
        result["bundle"] = build_bundle(outdir, dwg, main_r, refs)
    write_xref_table(outdir, dwg, refs, result["bundle"])
    return result


def build_bundle(outdir: Path, dwg: Path, main_r: dict, refs: list[dict]) -> str | None:
    """把修好的主图与参照按原相对路径拼成一个可直接打开的整包。
    参照用相对路径寻址，散着放会全断。"""
    b = outdir / "整包-已修复"
    if b.exists():
        shutil.rmtree(b, ignore_errors=True)
    b.mkdir(parents=True, exist_ok=True)
    n = 0
    if main_r.get("output_dwg"):
        shutil.copy2(main_r["output_dwg"], b / dwg.name)
        n += 1
    for r in refs:
        if r.get("fatal") or not r.get("output_dwg"):
            continue
        rel = r.get("rel")
        if not rel or rel.startswith(".."):
            r["bundle_note"] = "不在主图目录内，未纳入整包"
            continue
        dst = b / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(r["output_dwg"], dst)
        r["bundled"] = str(dst)
        n += 1
    return str(b) if n else None


def write_xref_table(outdir: Path, dwg: Path, refs: list[dict], bundle) -> None:
    if not refs:
        return
    L = [f"# 外部参照处理清单 · {dwg.name}\n",
         f"主图：`{dwg}`　参照数：{len(refs)}\n"]
    if bundle:
        L.append(f"整包（主图 + 修好的参照，相对路径已保持）：`{bundle}`\n")
    L += ["| 参照文件 | 相对路径 | 大小KB | 错误 | 警告 | 清理(空文字) | 结果 |",
          "|---|---|---|---|---|---|---|"]
    for r in refs:
        if r.get("fatal"):
            L.append(f"| {r.get('name')} | — | — | — | — | — | **{r['fatal']}** |")
            continue
        b = r["before"]
        a = r.get("after")
        cleaned = r.get("cleaned") or {}
        n_txt = cleaned.get("emptytext", 0) + cleaned.get("emptymtext", 0)
        res = "体检"
        if r.get("output_dwg"):
            res = "已清理" if a and a["counts"] != b["counts"] else "无需处理"
        L.append(f"| {Path(r['input']).name} | {r.get('rel') or '（目录外）'} | "
                 f"{Path(r['input']).stat().st_size // 1024} | "
                 f"{b['counts']['error']} | {b['counts']['warn']} | {n_txt} | {res} |")
    L.append("")
    L.append("> 参照的详细体检报告在同目录 `外部参照/` 下，文件名带参照名前缀。")
    (outdir / f"{dwg.stem}-外部参照清单.md").write_text("\n".join(L), encoding="utf-8")


def print_summary(r: dict):
    if r.get("fatal"):
        print("!! " + r["fatal"])
        return
    b, a = r["before"], r.get("after")
    print(f"\n{'='*58}\n修复前：{b['counts']['error']} 错误 / {b['counts']['warn']} 警告 / {b['counts']['info']} 提示")
    if a:
        print(f"修复后：{a['counts']['error']} 错误 / {a['counts']['warn']} 警告 / {a['counts']['info']} 提示")
        log = r.get("audit_log", {})
        print(f"AUDIT：检查到 {log.get('errors')} 个错误，修复 {log.get('fixed')} 个，删除 {log.get('deleted')} 个对象")
        c = r.get("cleaned") or {}
        if c:
            print(f"清理：零长线 {c.get('zerolen', 0)} / 空 TEXT {c.get('emptytext', 0)} / "
                  f"空 MTEXT {c.get('emptymtext', 0)}")
        print(f"文件：{r['size_before']:,} → {r['size_after']:,} 字节")
        print(f"输出：{r['output_dwg']}")
    xr = r.get("xrefs") or []
    if xr:
        miss = [x for x in xr if not x.get("exists")]
        print(f"外部参照：{len(xr)} 个" + (f"，其中 {len(miss)} 个找不到文件！" if miss else "，全部就位"))
    print("="*58)
    print(f"临时目录：{r['work']}（跑完自动删除，要留加 --keep-temp）")




# ---------------- 教育版（EDU）处理 ----------------
# EDU 标记是写进 DWG 数据库头的元数据，不在图元里，PURGE/WBLOCK 都动不了。
# 唯一可靠路径：DXF 往返重建数据库（DXF 是纯文本，不携带那个二进制标识位）。
# 注意 DWG 2018 的数据段是压缩的，字节扫描只能算一道不完全可靠的检查。
EDU_FEATURES = [
    (b"ACAD_EDU", "ACAD_EDU 标识"),
    (b"Educational", "Educational 字样"),
    (b"\xbd\xcc\xd3\xfd\xb0\xe6", "教育版（GBK/ANSI）"),
    (b"\xbd\xcc\xd3\xfd\xb0\xe6\xb2\xfa\xc6\xb7", "教育版产品（GBK）"),
]
# 刻意不收纯 b"EDU"：实测 7.2MB 的施工图里它命中 85 次，全部落在压缩数据段，
# 是 E/D/U 三字节的巧合。宁可漏报让人工确认，也不能天天误报。


# ---- 判定（2026-11-24 实测有效）----
# 教育版对话框在 accoreconsole 里会退化成一行「按 ENTER 键继续:」，
# 抓住这行就等于「AutoCAD 想弹框但被无头模式压平了」。
# 用 JTB 官方的「带戳 / 不带戳」成对样本校准过：带戳有这行，干净没有。
EDU_PROMPT_HINTS = ("按 ENTER 键继续", "Press ENTER to continue", "按 Enter 键继续")


_ROOM_CACHE: dict = {}


def _room_for(parent: Path, with_xref: bool) -> tuple:
    """同一个目录只搭一次「房间」（把主图 + 参照目录原样复制），批量判定时复用。
    参照路径是相对主图目录写的，所以必须保持同名同层级，否则参照解析不了。"""
    key = (str(parent), with_xref)
    if key in _ROOM_CACHE:
        return _ROOM_CACHE[key]
    work = Path(tempfile.mkdtemp(prefix="educhk_"))
    dirs = []
    if with_xref:
        for d in parent.iterdir():
            if d.is_dir() and ".dwg.Attach" in d.name:
                shutil.copytree(d, work / d.name)
                dirs.append(d.name)
    _ROOM_CACHE[key] = (work, dirs)
    return _ROOM_CACHE[key]


def clean_rooms():
    for work, _ in _ROOM_CACHE.values():
        shutil.rmtree(work, ignore_errors=True)
    _ROOM_CACHE.clear()


def check_edu(dwg: Path, timeout: int = 600, with_xref: bool = True) -> dict:
    """[已作废] 开图看「按 ENTER 键继续」判戳。

    2026-11-25 查明这个提示是 SYSVARMONITOR 的系统变量监控气泡被压平成一行，
    跟教育版无关：补齐 2367 个字体 + 所有参照后，干净的 建筑.dwg 与带戳的
    结构.dwg 弹得一模一样。判戳一律用 eduprint 的打印法，别再调这个。"""
    acc = find_acc()
    work, xref_dirs = _room_for(dwg.parent, with_xref)
    staged = work / "in.dwg"
    shutil.copy2(dwg, staged)
    scr = work / "noop.scr"
    scr.write_text('(setvar "FILEDIA" 0)\n(princ "[NO-DIALOG]")\n', encoding="ascii")
    rc, out = run_session(acc, staged, scr, timeout, work / "log.txt")
    hits = [h for h in EDU_PROMPT_HINTS if h in out]
    return {"dwg": str(dwg), "stamped": bool(hits), "hints": hits,
            "rc": rc, "xref_dirs": xref_dirs, "unresolved": out.count("未找到"),
            "unreliable": True}


def _judge(acc: str, dwg: Path, work: Path, tag: str, timeout: int) -> dict:
    """[已作废] 同 check_edu：抓到的是 SYSVARMONITOR 提示，不是教育版框。"""
    scr = work / f"judge_{tag}.scr"
    scr.write_text('(setvar "FILEDIA" 0)\n(princ "[NO-DIALOG]")\n', encoding="ascii")
    rc, out = run_session(acc, dwg, scr, timeout, work / f"judge_{tag}.log")
    hits = [h for h in EDU_PROMPT_HINTS if h in out]
    return {"stamped": bool(hits), "hints": hits, "rc": rc, "log": str(work / f"judge_{tag}.log")}


# accoreconsole 启动即失败的常见原因：2022+ 走的是 AdskLicensing，没登录/没激活会直接退出
LICENSE_HINTS = ("许可证", "License", "许可管理器", "Sign in", "未授权", "授权")


def _print_judge(dwg: Path, stampdir: Path, label: str, tag: str, timeout: int,
                 attach_from: Path | None = None) -> dict:
    """打印法判定（唯一可用）：打成 PDF，看四边有没有那行 AUTODESK 水印。

    2026-11-25 校准：同一张图，2020 内核打印，原件四边有 AUTODESK（0.6~0.8%），
    2022 另存件四边 0.0%。判据不看「有多黑」——干净图给排水右边能到 8.7%，
    那是图面内容。看的是四条边是否同时出现「8 个连通块、沿边占 15%」的同一行字。

    开图判定（_judge）已废弃：那个「按 ENTER 键继续」是 SYSVARMONITOR 的
    系统变量监控气泡被压平出来的，跟教育版无关，补齐全部字体参照后照样弹。

    注意：打印内核本身必须是非教育版授权，否则它自己会给每张图盖戳。
    """
    import eduprint
    r = eduprint.check_one(find_acc(), dwg, stampdir, 150, timeout,
                           attach_from=attach_from)
    if r.get("fatal"):
        return {"stamped": None, "method": "print", "fatal": r["fatal"],
                "log_tail": r.get("log_tail", "")}
    # eduprint 按临时文件名出图，这里换成正式名字留证
    for ext in (".pdf", ".png"):
        src = stampdir / f"{dwg.stem}-打印{ext}"
        if src.exists():
            src.replace(stampdir / f"{label}-打印-{tag}{ext}")
    v = r.get("verdict", {})
    return {"stamped": r["stamped"], "method": "print", "edges": r["edges"],
            "stats": r.get("edge_stats", {}), "worst": r["worst_pct"],
            "reason": v.get("reason", ""), "hits": v.get("hits", []),
            "pdf": str(stampdir / f"{label}-打印-{tag}.pdf"),
            "png": str(stampdir / f"{label}-打印-{tag}.png")}


def edu_strip(dwg: Path, outdir: Path, timeout: int, do_audit: bool = True,
              strip_acc: str | None = None, label: str | None = None,
              verify: str = "print") -> dict:
    """去教育版戳：打印判定 → 2022+ 内核打开并另存 → 再打印验收。

    唯一有官方背书的路径就是「2022 或更高版本打开图纸并保存」，
    所以这里不搞 DXF 往返、不删图元，只让新内核把文件重写一遍。
    原图一个字节都不动，成品另存成 `<名>-已去戳.dwg`。

    verify：
      print —— 前后各打印一次做判定与验收（默认，也是唯一可信的方式）
      none  —— 只清不验，快一倍，但结果没有证据，别拿这个交活
    """
    label = label or dwg.stem
    if strip_acc is None:
        # 允许「只判定不清戳」：本机没 2022 时先把带戳的挑出来，再决定装不装
        try:
            strip_acc = find_acc(EDU_STRIP_MIN_YEAR)
        except SystemExit:
            strip_acc = ""
    work = _new_workdir("edu_")
    staged = work / "in.dwg"
    shutil.copy2(dwg, staged)
    outdir.mkdir(parents=True, exist_ok=True)
    stampdir = outdir / "打印验收"
    stampdir.mkdir(parents=True, exist_ok=True)

    res = {"input": str(dwg), "label": label, "work": str(work), "verify": verify,
           "strip_acc": strip_acc, "size_before": dwg.stat().st_size}

    # ---- 处理前 ----
    before = (_print_judge(staged, stampdir, label, "前", timeout, dwg.parent)
              if verify != "none" else {"stamped": None, "method": "none"})
    res["before"] = before

    if before["stamped"] is False:
        res["skipped"] = ("判定为干净：打印件四边没有水印"
                          + (f"（边条暗像素 {before['worst']}% 来自图面内容）"
                             if (before.get("worst") or 0) > 0.05 else ""))
        return res
    if not strip_acc:
        res["skipped"] = "带戳，但本机没有 2022+ 内核，无法清（先装 AutoCAD 2022 或更高）"
        res["need_kernel"] = True
        return res

    # 2022 打开带戳图时，那个「学生版-检测到打印戳记」框同样会被压平成
    # 「按 ENTER 键继续:」，它会吃掉一行输入。所以：前面留两个空行吸收，
    # 关键命令（QSAVE）前再设一次 FILEDIA，别指望开头那几行还在。
    s = ["\n\n", PRELUDE]
    if do_audit:
        s.append('(vl-cmdf "_.AUDIT" "_Y")\n(princ "\\n[MARK-AUDIT]")\n')
    s.append('(setvar "FILEDIA" 0)\n(setvar "CMDDIA" 0)\n')
    s.append('(vl-cmdf "_.QSAVE")\n(princ "\\n[MARK-QSAVE]")\n')
    scr = work / "strip.scr"
    scr.write_text("".join(s), encoding="ascii")
    rc, out = run_session(strip_acc, staged, scr, timeout, work / "strip.log")

    res["rc"] = rc
    res["audited"] = "[MARK-AUDIT]" in out
    res["qsaved"] = "[MARK-QSAVE]" in out
    res["notes"] = [h for h in LICENSE_HINTS if h in out][:3]

    # ---- 处理后验收 ----
    after = (_print_judge(staged, stampdir, label, "后", timeout, dwg.parent)
             if verify != "none" else {"stamped": None, "method": "none"})
    res["after"] = after
    res["clean"] = after.get("stamped") is False

    out_dwg = outdir / f"{label}-已去戳.dwg"
    shutil.copy2(staged, out_dwg)
    res["output_dwg"] = str(out_dwg)
    res["size_after"] = out_dwg.stat().st_size
    return res


# 本工具自己的产物：批量扫描时要跳开，否则会把自己的成品当原图再处理一遍
PRODUCT_SUFFIXES = ("-已修复.dwg", "-已去教育版.dwg", "-已去戳.dwg")
OUTPUT_DIR_NAMES = ("dwg-check",)


def _is_product(f: Path) -> bool:
    return f.name.endswith(PRODUCT_SUFFIXES) or any(p in OUTPUT_DIR_NAMES for p in f.parts)


def _expand_targets(paths: list, include_attach: bool = False) -> list:
    """目录也能直接丢进来：递归收 *.dwg。

    默认跳过 *.dwg.Attach 参照缓存目录；include_attach=True 时连参照一起收
    ——戳会从参照传染到主图，只清主图等于白清。"""
    out = []
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() != ".dwg" or not f.is_file():
                    continue
                if _is_product(f):
                    continue
                if not include_attach and any(part.lower().endswith(".dwg.attach") for part in f.parts):
                    continue
                out.append(f)
        else:
            out.append(p)
    return out


def _verdict(d: dict | None) -> str:
    """把判定结果翻成人话。None = 打印失败，没定论。"""
    if not d:
        return "未判定"
    s = d.get("stamped")
    if s is True:
        return "带戳"
    if s is False:
        return "干净"
    return "无法判定（打印失败）"


def print_edu_strip(r: dict):
    print(f"\n{'-' * 58}")
    print(f"{r['label']}")
    if r.get("skipped"):
        print(f"  {r['skipped']}")
        return
    b, a = r.get("before", {}), r.get("after", {})
    print(f"  清戳前：{_verdict(b)}　清戳后：{_verdict(a)}"
          + (" ✓" if r.get("clean") else " ✗"))
    if b.get("reason"):
        print(f"  处理前依据：{b['reason']}")
    if a.get("reason"):
        print(f"  处理后依据：{a['reason']}"
              + (f"　边条暗像素 {b['worst']}% → {a['worst']}%"
                 if b.get("worst") is not None and a.get("worst") is not None else ""))
    print(f"  内核：{r['strip_acc']}")
    if not r.get("qsaved"):
        print("  !! QSAVE 没跑到，另存可能失败（看 " + r["work"] + r"\\strip.log）")
    if r.get("notes"):
        print(f"  !! 2022 疑似授权问题：{'/'.join(r['notes'])}")
    if a.get("fatal"):
        print(f"  !! 打印验收失败：{a['fatal']}")
    print(f"  文件：{r['size_before']:,} → {r['size_after']:,} 字节")
    if a.get("png"):
        print(f"  验收图：{a['png']}")
    print(f"  输出：{r['output_dwg']}")


def write_edu_strip_report(outdir: Path, rows: list, strip_acc: str,
                           verify: str = "print") -> Path:
    ok = sum(1 for r in rows if r.get("clean"))
    skip = sum(1 for r in rows if r.get("skipped") and not r.get("need_kernel"))
    need = sum(1 for r in rows if r.get("need_kernel"))
    bad = len(rows) - ok - skip - need
    L = ["# 去教育版戳 报告\n",
         f"清戳内核：`{strip_acc or '无（未安装 2022+）'}`　验收方式：`{verify}`\n",
         f"合计 {len(rows)} 张：已清 {ok} / 本来就干净 {skip} / 带戳待装内核 {need} / 失败 {bad}\n",
         "| 文件 | 处理前 | 处理后 | 边条暗像素 | 大小KB | 结果 |",
         "|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("skipped"):
            state = "带戳，缺 2022+ 内核" if r.get("need_kernel") else "干净"
            L.append(f"| {r['label']} | {state} | — | — | {r['size_before'] // 1024} | 跳过 |")
            continue
        b, a = r.get("before", {}), r.get("after", {})
        edge = (f"{b['worst']} → {a['worst']}"
                if b.get("worst") is not None and a.get("worst") is not None else "—")
        res = "已去戳" if r.get("clean") else "**仍带戳，需人工**"
        L.append(f"| {r['label']} | {_verdict(b)} | {_verdict(a)} | {edge} | "
                 f"{r['size_after'] // 1024} | {res} |")
    L.append("")
    L.append("> 判据：打印成 PDF 后渲染，看最外 10% 边条。戳印是**四边各一行同样的字**，"
             "每边约 8 个连通块（AUTODESK）、沿边占 15% 长度、暗像素 0.6~0.8%。")
    L.append("> 只有一两条边有暗像素的是图面内容伸进边条（图框、小图块），不是戳——"
             "干净图给排水右边能到 8.7%，所以不能按「黑多少」判。")
    L.append("> 注意：`educheck` 的「按 ENTER 键继续」判据已废弃，那是 SYSVARMONITOR 的"
             "系统变量提示，跟教育版无关。")
    p = outdir / "去戳报告.md"
    p.write_text("\n".join(L), encoding="utf-8")
    return p


def detect_edu(path: Path) -> list:
    """[已废弃] 字节特征扫描。DWG 数据段是压缩的，带戳样本里也搜不到，
    实测官方 EDU 样本'未检出'。保留只为兼容，判定一律用 check_edu()。
    返回命中列表 [(bytes, 说明, 次数, 首次偏移)]"""
    try:
        b = path.read_bytes()
    except Exception:
        return []
    hits = []
    for feat, desc in EDU_FEATURES:
        n = b.count(feat)
        if n:
            hits.append((feat, desc, n, b.find(feat)))
    return hits


def strip_edu(dwg: Path, outdir: Path, timeout: int, force: bool = False) -> dict:
    """DXF 往返重建数据库，输出到新文件，原图不动"""
    acc = find_acc()
    work = Path(tempfile.gettempdir()) / "dwgfix" / f"edu_{time.strftime('%H%M%S')}_{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    src = work / "in.dwg"
    shutil.copy2(dwg, src)
    outdir.mkdir(parents=True, exist_ok=True)

    res = {"input": str(dwg), "before": detect_edu(dwg), "work": str(work), "forced": force}

    # 步骤 1：DWG -> DXF
    scr1 = work / "to_dxf.scr"
    scr1.write_text(PRELUDE + dxfout((work / "raw.dxf").as_posix(), "RAW")
                    + r'(princ "\n[MARK-END]")' + "\n", encoding="ascii")
    run_session(acc, src, scr1, timeout, work / "s1.log")
    raw = work / "raw.dxf"
    if not raw.exists():
        res["fatal"] = "DXF 导出失败，文件可能损坏"
        return res

    # 步骤 2：用 ezdxf 重写数据库（新 header，剥掉不认识的元数据）
    import ezdxf
    doc = ezdxf.readfile(str(raw))
    clean_dxf = work / "clean.dxf"
    doc.saveas(str(clean_dxf))
    res["entities_before"] = len(doc.modelspace())

    # 步骤 3：DXF -> DWG（SAVEAS 必须先给格式再给文件名，反序会挂死）
    scr2 = work / "to_dwg.scr"
    scr2.write_text(PRELUDE + f'(vl-cmdf "_.SAVEAS" "2018" "{(work / "out.dwg").as_posix()}")\n'
                    + r'(princ "\n[MARK-END]")' + "\n", encoding="ascii")
    run_session(acc, clean_dxf, scr2, timeout, work / "s2.log")

    produced = work / "out.dwg"
    if not produced.exists():
        res["fatal"] = "回写 DWG 失败（SAVEAS 未产出文件）"
        return res

    out = outdir / f"{dwg.stem}-已去教育版.dwg"
    shutil.copy2(produced, out)
    res["output_dwg"] = str(out)
    res["after"] = detect_edu(out)
    res["size_before"] = dwg.stat().st_size
    res["size_after"] = out.stat().st_size
    res["clean"] = len(res["before"]) > 0 and len(res["after"]) == 0
    return res


def print_edu(r: dict):
    b, a = r.get("before") or [], r.get("after") or []
    print(f"\n{'=' * 58}")
    print(f"输入：{r['input']}")
    if r.get("fatal"):
        print("!! " + r["fatal"]); print("=" * 58); return
    if not b and not r.get("forced"):
        print("检测结果：未发现教育版标记（字节层面）")
        print("注意：DWG 数据库是压缩的，本检测不能证明绝对没有；"
              "最可靠的确认是在 AutoCAD 里打开看有无『教育版产品制作』提示。")
        print("=" * 58); return
    if r.get("forced") and not b:
        print("强制往返模式：未检出标记，仍执行了一次 DXF 往返（用于验证链路）")
    else:
        print("检测到 " + str(len(b)) + " 类标记：" + "、".join(f"{d}×{n}" for _, d, n, _ in b))
        print("清洗后：" + ("已清除" if not a else "仍有残留 → " + str([d for _, d, n, _ in a])))
    print(f"文件：{r['size_before']:,} → {r['size_after']:,} 字节")
    print(f"输出：{r.get('output_dwg')}")
    print("=" * 58)


def main():
    ap = argparse.ArgumentParser(description="DWG 修复 / 核查 / 清理")
    ap.add_argument("mode", choices=["audit", "repair", "all", "deedu", "educheck", "edustrip"])
    ap.add_argument("dwg", nargs="+", help="一个或多个 DWG 路径；educheck/edustrip 支持批量与目录")
    ap.add_argument("--out", default=None, help="输出目录，默认与 DWG 同级 dwg-check/")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--purge-rounds", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="deedu 时即使未检出也强制走一遍往返")
    ap.add_argument("--no-clean", action="store_true", help="repair 时只做 AUDIT/PURGE/OVERKILL")
    ap.add_argument("--no-audit", action="store_true", help="edustrip 时只另存，不跑 AUDIT")
    ap.add_argument("--verify", choices=["print", "none"], default="print",
                    help="edustrip 的验收方式：print=前后各打一次图（默认，唯一可信），"
                         "none=只清不验（快，但没有证据）")
    ap.add_argument("--no-xref", action="store_true",
                    help="不处理外部参照（educheck 时也不带参照目录复制）。默认会一并处理")
    ap.add_argument("--xref-depth", type=int, default=1,
                    help="外部参照递归层数：1=只处理主图的直接参照（默认），2=参照的参照也处理")
    ap.add_argument("--keep-temp", action="store_true",
                    help="保留 %%TEMP%%\\dwgfix 下的中间件（默认跑完删除）")
    ap.add_argument("--acc", default=None, metavar="PATH",
                    help="accoreconsole.exe 路径（判定/打印用）。默认自动发现最老版本；"
                         f"装在非标准位置也可用环境变量 {ENV_ACC}")
    ap.add_argument("--strip-acc", default=None, metavar="PATH",
                    help=f"清教育版戳用的 accoreconsole 路径，必须 {EDU_STRIP_MIN_YEAR}+。"
                         "默认自动发现最高的合格版本")
    ns = ap.parse_args()

    global _ACC_OVERRIDE, _STRIP_ACC_OVERRIDE
    _ACC_OVERRIDE = ns.acc
    _STRIP_ACC_OVERRIDE = ns.strip_acc
    if ns.strip_acc:
        m = _YEAR_RE.search(ns.strip_acc)
        if m and int(m.group(1)) < EDU_STRIP_MIN_YEAR:
            raise SystemExit(
                f"--strip-acc 指向 AutoCAD {m.group(1)}，但清戳需要 {EDU_STRIP_MIN_YEAR}+。"
                f"（{m.group(1)} 实测洗不掉戳）")

    files = [Path(p).resolve() for p in ns.dwg]
    for f in files:
        if not f.exists():
            raise SystemExit(f"文件不存在：{f}")

    if ns.mode == "edustrip":
        outdir = Path(ns.out) if ns.out else files[0].parent / "dwg-check"
        targets = _expand_targets(files, include_attach=not ns.no_xref)
        if not targets:
            raise SystemExit("没有找到任何 .dwg")
        judge_acc = find_acc()                      # 打印判定用的内核（2020，商业版）
        try:
            strip_acc = find_acc(EDU_STRIP_MIN_YEAR)    # 清戳内核：必须 2022+
        except SystemExit:
            strip_acc = ""
        if strip_acc:
            print(f"判定内核（打印）：{judge_acc}\n清戳内核：{strip_acc}")
        else:
            print(f"判定内核（打印）：{judge_acc}\n清戳内核：无（本机没有 {EDU_STRIP_MIN_YEAR}+，只判定不清戳）")
        if ns.verify == "none":
            print("!! --verify none：只清不验，结果没有证据，别拿这个交活")
        n_ref = sum(1 for t in targets
                    if any(part.lower().endswith(".dwg.attach") for part in t.parts))
        print(f"目标：{len(targets)} 张" + (f"（其中外部参照 {n_ref} 张）" if n_ref else "")
              + "，串行处理（accoreconsole 并发会抢 profile，不能并行）")
        rows = []
        used: set = set()
        for t in targets:
            label = t.stem
            if label in used:
                label = f"{_attach_tag(t) or t.parent.name}_{label}"
            used.add(label)
            r = edu_strip(t, outdir, ns.timeout, do_audit=not ns.no_audit,
                          strip_acc=strip_acc, label=label, verify=ns.verify)
            rows.append(r)
            print_edu_strip(r)
            if not ns.keep_temp:
                shutil.rmtree(str(r["work"]), ignore_errors=True)
        rep = write_edu_strip_report(outdir, rows, strip_acc, ns.verify)
        (outdir / "edustrip.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        ok = sum(1 for r in rows if r.get("clean"))
        skip = sum(1 for r in rows if r.get("skipped") and not r.get("need_kernel"))
        need = sum(1 for r in rows if r.get("need_kernel"))
        bad = len(rows) - ok - skip - need
        print(f"\n{'=' * 58}\n合计 {len(rows)} 张：已去戳 {ok}，本来就干净 {skip}，"
              f"带戳待装内核 {need}，失败 {bad}\n报告：{rep}")
        if bad:
            sys.exit(f"\n{bad} 张处理失败，见上方错误信息")
        return

    dwg = files[0]
    outdir = Path(ns.out) if ns.out else dwg.parent / "dwg-check"

    if ns.mode == "educheck":
        print("!! educheck 的「按 ENTER 键继续」判据已作废（那是 SYSVARMONITOR 提示），"
              "结果不可信；要定论请用 eduprint.py check")
        rows = []
        for f in files:
            r = check_edu(f, ns.timeout, with_xref=not ns.no_xref)
            rows.append(r)
            tag = "★★ 报带戳" if r["stamped"] else "  报干净"
            extra = f"（参照目录 {len(r['xref_dirs'])} 个）" if r["xref_dirs"] else ""
            print(f"{tag}  {f.name}{extra}")
        print(f"\n合计 {len(rows)} 张，带戳 {sum(r['stamped'] for r in rows)} 张")
        (outdir / "educheck.json").parent.mkdir(parents=True, exist_ok=True)
        (outdir / "educheck.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        clean_rooms()
        return

    if ns.mode == "deedu":
        r = strip_edu(dwg, outdir, ns.timeout, ns.force)
        (outdir / f"{dwg.stem}-deedu.json").write_text(
            json.dumps(r, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print_edu(r)
        return

    do_repair = ns.mode in ("repair", "all")
    do_clean = (not ns.no_clean) and do_repair
    seen: set = set()
    failed: list = []
    for i, dwg in enumerate(files):
        if len(files) > 1:
            print(f"\n### {dwg.name}", flush=True)
        res = process_with_xrefs(dwg, outdir, do_repair, do_clean, ns.timeout,
                                 ns.purge_rounds, with_xref=not ns.no_xref,
                                 max_depth=ns.xref_depth, seen=seen)
        (outdir / f"{dwg.stem}-report.json").write_text(
            json.dumps(res, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print_summary(res["main"])
        if res["main"].get("fatal"):
            failed.append(dwg.name)
        if res["refs"]:
            ok = sum(1 for r in res["refs"] if not r.get("fatal"))
            print(f"参照：{len(res['refs'])} 个已处理（{ok} 个正常）"
                  + (f"，整包在 {res['bundle']}" if res.get("bundle") else ""))
            print(f"清单：{outdir / (dwg.stem + '-外部参照清单.md')}")
        # 临时目录里是 DXF 中间件，一批图能堆几百 MB。产物都拷到 outdir 了，默认删掉。
        if not ns.keep_temp:
            for r in [res["main"]] + res["refs"]:
                if r.get("work"):
                    shutil.rmtree(str(r["work"]), ignore_errors=True)

    # 有文件没处理成功就返回非 0，否则批处理和 CI 判断不出成败
    if failed:
        sys.exit(f"\n{len(failed)} 个文件处理失败：{'、'.join(failed)}")


if __name__ == "__main__":
    main()
