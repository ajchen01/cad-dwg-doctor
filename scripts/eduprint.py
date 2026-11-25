#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""eduprint.py — 用「打印后看四边水印」判定教育版戳记（唯一可信的判据）

背景
----
老判据 `educheck` 靠 accoreconsole 日志里的「按 ENTER 键继续」。

2026-11-25 查清了这个提示的真身：它出现在「正在重生成模型 / CoreHeartBeat」之后，
紧跟的是「已从首选值更改 2 个监视系统变量」——是 **SYSVARMONITOR 的系统变量监控
气泡**被无头控制台压平成的提示，跟教育版毫无关系。补齐参照、补齐全部字体后，
干净图和带戳图弹得一字不差，日志完全一样。所以开图判据彻底作废，
不要再拿它判戳。

打印法才是定论：
    同一张图，2020 内核打印 —— 原件四边有 AUTODESK 水印，2022 另存件四边干净。

判据（用 3 张带戳 + 5 张干净样本标定）
    戳印是**一行同样的字反复出现在边条里**，每边 ≈8 个连通块（AUTODESK 八个字母）、
    沿边占 8~15%、暗像素 0.6~0.8%，且各边深浅一致。
    图面内容伸进边条的样子完全不同：有的是 5 个块占 0.2%，有的几十上百块、占比高到 9%。
    → 只看「黑多少」必然误判（边条 9% 的反而是干净图）；要看形态。
    → 也不能要求四条边全露：图面内容会压住水印，所以判「≥2 条边命中且深浅接近」。

用法
----
    python eduprint.py check  "图.dwg" [--out 报告目录] [--dpi 150]
    python eduprint.py check  "目录" --out 报告目录

依赖：pymupdf、opencv-python、pillow、numpy。
需要本机有 AutoCAD 2020+ 的 accoreconsole（打印用，任何版本都行，但要是非教育版授权）。
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

# 边条宽度占页面短边的比例；水印就在这个范围内
EDGE_RATIO = 0.10
# 打印超时（秒），大图放宽
PLOT_TIMEOUT = 600

# ---- 水印指纹（用 3 张带戳 + 5 张干净样本标定）----
# 带戳的边条形态高度一致：
#   每边约 8 个连通块 = A U T O D E S K 八个字母
#   沿边占 8~15% 长度，暗像素 0.6~0.8%，文字有厚度（暗行约 19 行 @ dpi150）
# 干净图的边条暗像素来自图面内容，特征完全不同：
#   有的只有一条边（5 块 / 0.22%），有的块数几十上百、占比高到 9%（图框和平面图）。
# 所以判据不是「有多黑」，而是「这条边是不是那行 8 个字母的字」。
#
# 为什么是「至少 2 条边」而不是「4 条」：图面内容会压住水印。实测有张图右边被
# 平面图盖满，只剩 3 条边露出水印；要求四条边同时成立会漏判。
# 但也不能放宽到 1 条——单条边偶然长得像字的情况太多，所以加一条约束：
# 成立的各边深浅必须接近（max/min ≤ WM_SPREAD），水印是同时盖上去的，深浅自然一致。
WM_PCT_RANGE = (0.40, 1.30)     # 单边暗像素占比区间（%），水印恒在 0.6~0.8
WM_COLCOV_RANGE = (0.05, 0.32)  # 暗像素沿边覆盖的长度比例
WM_NCOMP_RANGE = (6, 14)        # 连通块个数（8 个字母，容错放宽）
WM_MIN_ROWS = 8                 # 文字有厚度；图框线只有一两行
WM_MIN_EDGES = 2                # 至少几条边成立才算戳
WM_SPREAD = 1.6                 # 成立各边占比的最大/最小比，超过就不像同一行字

# 这些 SHX 找不到时会弹替换框，复制到工作目录减少噪音
COMMON_SHX = ("hztxt.shx", "china.shx", "tssdchn.shx", "tssdeng.shx", "!hztxt.shx",
              "hzfs.shx", "txt.shx", "simplex.shx", "romans.shx")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dwgpipe import find_acc, _is_product, _new_workdir  # noqa: E402  引擎发现只维护一份，别在这儿重复实现


def _render_deps():
    """PDF 渲染依赖：pymupdf（新包名）或 fitz（老包名）二选一。
    缺了就直说要装什么，别抛一坨 ImportError 让人猜。"""
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf          # 1.23 以前叫 fitz
        except ImportError:
            raise SystemExit(
                "缺依赖：pip install pymupdf opencv-python pillow numpy\n"
                "（打印出来的 PDF 要靠它渲染成图再量边条暗像素）")
    from PIL import Image
    return pymupdf, Image


def _page_image(pdf: Path, dpi: int):
    pymupdf, Image = _render_deps()
    doc = pymupdf.open(str(pdf))
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


def _band_stats(mask, name: str) -> dict:
    """单条边的形态：暗像素占比、连通块数、沿边覆盖长度、暗行数。

    mask 已按「沿边方向在列、内侧方向在行」摆好（左右两边的图先转置），
    这样四条边的统计口径才一致。"""
    import cv2
    import numpy as np

    h, w = mask.shape
    area = int(mask.sum())
    d = {"side": name, "pct": round(area / (h * w) * 100, 3),
         "ncomp": 0, "colcov": 0.0, "rows": 0, "maxw": 0.0}
    if not area:
        return d
    n, _lab, st, _c = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    areas = st[1:, cv2.CC_STAT_AREA]
    keep = areas >= 5                       # 去孤立噪点
    d["ncomp"] = int(keep.sum())
    if keep.any():
        d["maxw"] = round(float(st[1:, cv2.CC_STAT_WIDTH][keep].max() / w), 3)
    cols = np.where(mask.any(axis=0))[0]
    d["colcov"] = round(len(cols) / w, 3) if len(cols) else 0.0
    d["rows"] = int(mask.any(axis=1).sum())
    return d


def _edge_stats(pdf: Path, dpi: int) -> tuple[dict, dict]:
    """返回 ({边: 形态}, 判定)。判定 = 四条边是否都长得像那行 AUTODESK 水印。"""
    import numpy as np

    m_img = _page_image(pdf, dpi)
    g = np.asarray(m_img.convert("L"))
    h, w = g.shape
    m = max(8, int(min(h, w) * EDGE_RATIO))
    masks = {
        "top": g[:m, :] < 128,
        "bottom": g[-m:, :] < 128,
        "left": (g[:, :m] < 128).T,          # 转置，让「沿边」永远是列方向
        "right": (g[:, -m:] < 128).T,
    }
    stats = {k: _band_stats(v, k) for k, v in masks.items()}

    def wm_loose(s: dict) -> bool:
        return (WM_PCT_RANGE[0] <= s["pct"] <= WM_PCT_RANGE[1]
                and WM_COLCOV_RANGE[0] <= s["colcov"] <= WM_COLCOV_RANGE[1]
                and WM_NCOMP_RANGE[0] <= s["ncomp"] <= WM_NCOMP_RANGE[1]
                and s["rows"] >= WM_MIN_ROWS)

    def wm_strict(s: dict) -> bool:
        """精确命中八个字母：块数 6~10、暗 0.55~0.9%、沿边 5~20%。
        内容把其余三条边全压住时，只剩一条边也得认出来。"""
        return (6 <= s["ncomp"] <= 10
                and 0.55 <= s["pct"] <= 0.90
                and 0.05 <= s["colcov"] <= 0.20
                and s["rows"] >= 12)

    loose = [k for k, s in stats.items() if wm_loose(s)]
    strict = [k for k, s in stats.items() if wm_strict(s)]
    hit_pcts = [stats[k]["pct"] for k in loose]
    spread = (max(hit_pcts) / min(hit_pcts)) if len(hit_pcts) >= 2 else 0.0

    # 单边精确命中即认；多条边宽口径命中时，深浅还得一致（同一行字）
    stamped = bool(strict) or (len(loose) >= WM_MIN_EDGES and spread <= WM_SPREAD)
    hits = sorted(set(loose) | set(strict))
    worst = max(s["pct"] for s in stats.values())
    if stamped:
        reason = (f"{len(strict)} 条边精确命中、共 {len(hits)} 条边有同形态文字"
                  f"（{'/'.join(hits)}），判为带戳")
    elif not loose:
        reason = "无边的暗像素形态像水印"
    else:
        reason = (f"仅 {len(loose)} 条边（{'/'.join(loose)}）像文字"
                  + (f"且深浅不一（{spread} 倍）" if spread > WM_SPREAD else "")
                  + "，判为图面内容而非戳")
    return stats, {"stamped": stamped, "worst_pct": round(worst, 3),
                   "hits": hits, "strict": strict,
                   "spread": round(spread, 2), "reason": reason}


def _plot(acc: str, dwg: Path, work: Path, pdf: Path, timeout: int) -> tuple[int, str]:
    scr = work / "plot.scr"
    # 参数序列（2020 实测）：
    #   详细配置N / 布局名(空=模型) / 页面设置名(空) / 设备 / 文件名 / 保存设置N / 继续打印Y
    scr.write_text(
        '(setvar "FILEDIA" 0)\n'
        '(setvar "CMDDIA" 0)\n'
        f'(vl-cmdf "_.-plot" "N" "" "" "DWG To PDF.pc3" "{pdf.as_posix()}" "N" "Y")\n'
        '(princ "\\n[MARK-PLOT-DONE]")\n'
        '(vl-cmdf "_.QUIT" "_Y")\n',
        encoding="ascii",
    )
    env = dict(os.environ)
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    try:
        p = subprocess.run(
            [acc, "/i", str(dwg), "/s", str(scr)],
            cwd=str(work), capture_output=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-16-le", "replace")
        partial += (exc.stderr or b"").decode("utf-16-le", "replace")
        return 124, "打印超时\n" + partial
    log = p.stdout.decode("utf-16-le", "ignore")
    return p.returncode, log


def check_one(acc: str, dwg: Path, outdir: Path, dpi: int, timeout: int,
              attach_from: Path | None = None) -> dict:
    # 工作目录必须是纯英文，accoreconsole 读脚本按本地 ANSI，中文路径会乱码
    work = _new_workdir("print_")
    work.mkdir(parents=True, exist_ok=True)
    res = {"input": str(dwg), "work": str(work)}

    staged = work / "in.dwg"
    try:
        shutil.copy2(dwg, staged)
    except Exception as e:
        res["fatal"] = f"复制失败: {e}"
        return res

    # 参照目录一起搬，否则「参照未找到」框会混进来（虽不影响水印判定，但会污染日志）
    # attach_from：被检文件是副本（临时目录里）时，从原图所在目录找 .dwg.Attach
    for att in (attach_from or dwg.parent).glob("*.dwg.Attach"):
        try:
            shutil.copytree(att, work / att.name, dirs_exist_ok=True)
        except Exception:
            pass

    # 常用字体补进工作目录，减少替换提示。字体目录就在引擎自己的安装目录下，
    # 从 acc 路径推导即可，不要硬编码版本号和盘符。
    font_dirs = [Path(acc).parent / "Fonts"]
    for fd in font_dirs:
        if not fd.is_dir():
            continue
        for name in COMMON_SHX:
            src = fd / name
            if src.exists() and not (work / name).exists():
                try:
                    shutil.copy2(src, work / name)
                except Exception:
                    pass

    # OpenCV 缺失应返回可汇总的失败，而不是在整批中途抛出 ImportError。
    try:
        import cv2
        import numpy
    except ImportError as exc:
        res["fatal"] = f"缺少打印分析依赖：{exc}；请安装 requirements.txt"
        return res
    pdf = work / "out.pdf"
    rc, log = _plot(acc, staged, work, pdf, timeout)
    (work / "session-plot.log").write_text(log, encoding="utf-8")
    res["rc"] = rc
    if rc != 0 or not pdf.is_file():
        res["fatal"] = (f"打印会话失败或超时（退出码 {rc}），未作判定" if rc != 0
                        else "未生成 PDF（打印失败）")
        res["log_tail"] = "\n".join(log.splitlines()[-6:])
        return res

    try:
        stats, verdict = _edge_stats(pdf, dpi)
    except Exception as exc:
        res["fatal"] = f"PDF 渲染或分析失败：{exc}"
        return res
    res["pdf"] = str(pdf)
    res["edges"] = {k: v["pct"] for k, v in stats.items()}   # 兼容老字段：只给百分比
    res["edge_stats"] = stats
    res["worst_pct"] = verdict["worst_pct"]
    res["stamped"] = verdict["stamped"]
    res["verdict"] = verdict
    res["size_kb"] = round(pdf.stat().st_size / 1024, 1)

    outdir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(pdf, outdir / f"{dwg.stem}-打印.pdf")
    except Exception:
        pass
    # 存一张渲染图，便于人工抽查
    try:
        _page_image(pdf, 110).save(outdir / f"{dwg.stem}-打印.png")
    except Exception:
        pass
    return res


def collect(paths: list[str]) -> list[Path]:
    out = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out += sorted(x for x in p.rglob("*.dwg") if x.is_file() and not _is_product(x) and not any(part.lower().endswith(".dwg.attach") for part in x.parts))
        elif p.is_file() and p.suffix.lower() == ".dwg":
            out.append(p)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="打印判教育版戳记")
    ap.add_argument("mode", choices=["check"])
    ap.add_argument("target", nargs="+", help="dwg 文件或目录")
    ap.add_argument("--out", default="", help="报告与 PDF/PNG 输出目录")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--timeout", type=int, default=PLOT_TIMEOUT)
    ap.add_argument("--acc", default=None, metavar="PATH",
                    help="accoreconsole.exe 路径。默认自动发现最老版本"
                         "（新版本可能是教育版授权，打印会给每张图盖戳）")
    ns = ap.parse_args()

    files = collect(ns.target)
    if not files:
        raise SystemExit("没有找到 dwg")
    outdir = Path(ns.out) if ns.out else (files[0].parent / "eduprint")
    outdir.mkdir(parents=True, exist_ok=True)
    files = list(dict.fromkeys(f.resolve() for f in files))
    acc = find_acc(acc=ns.acc)
    print(f"打印内核：{acc}\n目标 {len(files)} 张，输出：{outdir}")

    rows = []
    for index, f in enumerate(files, 1):
        print(f"\n… {f.name}", flush=True)
        r = check_one(acc, f, outdir / f"{index:04d}-{f.stem}", ns.dpi, ns.timeout)
        rows.append(r)
        if r.get("fatal"):
            print(f"   ✗ {r['fatal']}")
        else:
            tag = "疑似带戳" if r["stamped"] else "本次打印未检出"
            print(f"   → {tag}  {r['verdict']['reason']}  （最暗边 {r['worst_pct']}%）")

    ok = sum(1 for r in rows if not r.get("fatal") and r["stamped"])
    clean = sum(1 for r in rows if not r.get("fatal") and not r["stamped"])
    bad = len(rows) - ok - clean

    L = ["# 教育版戳记判定（打印法）\n",
         f"打印内核：`{acc}`\n",
         f"合计 {len(rows)} 张：带戳 {ok} / 干净 {clean} / 失败 {bad}\n",
         "",
         f"> 判据：打印成 PDF 后渲染，看最外 {int(EDGE_RATIO * 100)}% 边条。",
         "> 戳印是同一行 AUTODESK 文字：每边约 8 个连通块、沿边占 8~15%、"
         f"暗像素 {WM_PCT_RANGE[0]}~{WM_PCT_RANGE[1]}%，且各边深浅接近。",
         f"> **至少 {WM_MIN_EDGES} 条边命中**即判带戳——图面内容会压住水印，"
         "要求四条边全露出来会漏判。",
         "> 光看「边条有多黑」必然误判：干净图的图框能让边条暗到 9%，那是内容不是戳。",
         "",
         "| 文件 | 上 | 下 | 左 | 右 | 块数(上/下/左/右) | 结果 |",
         "|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("fatal"):
            L.append(f"| {Path(r['input']).name} | — | — | — | — | — | 失败：{r['fatal']} |")
            continue
        e = r["edges"]
        st = r.get("edge_stats") or {}
        nc = "/".join(str(st[k]["ncomp"]) for k in ("top", "bottom", "left", "right")) if st else "—"
        L.append(f"| {Path(r['input']).name} | {e['top']} | {e['bottom']} | "
                 f"{e['left']} | {e['right']} | {nc} | "
                 f"{'**疑似带戳**' if r['stamped'] else '本次打印未检出'} |")
    rep = outdir / "戳记判定-打印法.md"
    rep.write_text("\n".join(L), encoding="utf-8")
    (outdir / "eduprint.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'=' * 52}\n合计 {len(rows)}：带戳 {ok}，干净 {clean}，失败 {bad}\n报告：{rep}")

    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
