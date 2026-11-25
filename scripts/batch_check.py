# -*- coding: utf-8 -*-
"""批量体检 / 修复一个目录下的 DWG，汇总成一张总表。

只扫指定目录这一层（默认不递归进 *.dwg.Attach 这类参照缓存目录），
要看子目录加 --recursive。

外部参照默认**一并处理**（同一套体检/修复/清理），同一个参照被多张主图共用时只跑一次；
不想要加 --no-xref。

用法
    python batch_check.py <目录> [--repair] [--out DIR] [--recursive] [--no-xref]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dwgpipe import PRODUCT_SUFFIXES, process_with_xrefs  # noqa: E402


def collect(root: Path, recursive: bool) -> list[Path]:
    it = root.rglob("*.dwg") if recursive else root.glob("*.dwg")
    out = []
    for p in it:
        if not recursive and any(part.lower().endswith(".attach") for part in p.parts):
            continue
        # 本工具自己的产物不要再当原图扫一遍（顺手也会把目录里的历史副本排除掉）
        if p.name.endswith(PRODUCT_SUFFIXES):
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.stat().st_size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--repair", action="store_true", help="顺带修复并输出修后图")
    ap.add_argument("--out", default=None)
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--no-xref", action="store_true", help="不处理外部参照（默认处理）")
    ap.add_argument("--xref-depth", type=int, default=1)
    ap.add_argument("--keep-temp", action="store_true")
    ns = ap.parse_args()

    root = Path(ns.dir).resolve()
    outdir = Path(ns.out) if ns.out else root / "dwg-check"
    files = collect(root, ns.recursive)
    if not files:
        raise SystemExit("没找到 dwg")

    rows, xrows = [], []
    seen: set = set()
    for p in files:
        t0 = time.time()
        print(f"… {p.name}", flush=True)
        try:
            res = process_with_xrefs(p, outdir, do_repair=ns.repair, do_clean=ns.repair,
                                     timeout=ns.timeout, with_xref=not ns.no_xref,
                                     max_depth=ns.xref_depth, seen=seen)
            r = res["main"]
        except Exception as exc:
            rows.append(dict(name=p.name, kb=p.stat().st_size // 1024, fatal=f"{type(exc).__name__}: {exc}"))
            continue
        for xr in res["refs"]:
            if xr.get("fatal"):
                xrows.append(dict(name=xr.get("name"), owner=p.name, fatal=xr["fatal"]))
                continue
            b, a = xr["before"], xr.get("after")
            xrows.append(dict(
                name=Path(xr["input"]).name, owner=p.name, rel=xr.get("rel"),
                kb=Path(xr["input"]).stat().st_size // 1024,
                err=b["counts"]["error"], warn=b["counts"]["warn"], info=b["counts"]["info"],
                err_after=a["counts"]["error"] if a else None,
                cleaned=(xr.get("cleaned") or {}).get("emptytext", 0)
                        + (xr.get("cleaned") or {}).get("emptymtext", 0),
                out=xr.get("output_dwg"),
            ))
        if r.get("fatal"):
            rows.append(dict(name=p.name, kb=p.stat().st_size // 1024, fatal=r["fatal"]))
            if not ns.keep_temp:
                for x in [r] + res["refs"]:
                    if x.get("work"):
                        shutil.rmtree(str(x["work"]), ignore_errors=True)
            continue
        b = r["before"]
        a = r.get("after")
        top = [f"{f['title']}×{f['count']}" for f in b["findings"] if f["level"] != "info"][:3]
        rows.append(dict(
            name=p.name, kb=p.stat().st_size // 1024,
            ver=b["stats"].get("dxf_version", ""),
            layers=b["stats"].get("layers"), proxy=b["stats"].get("proxy"),
            err=b["counts"]["error"], warn=b["counts"]["warn"], info=b["counts"]["info"],
            err_after=a["counts"]["error"] if a else None,
            xrefs=len(b["stats"].get("xrefs") or []),
            top="; ".join(top) or "—",
            secs=round(time.time() - t0, 1),
            out=r.get("output_dwg"),
            bundle=res.get("bundle"),
        ))
        if not ns.keep_temp:
            for x in [r] + res["refs"]:
                if x.get("work"):
                    shutil.rmtree(str(x["work"]), ignore_errors=True)

    (outdir / "batch.json").write_text(json.dumps({"main": rows, "xref": xrows},
                                                  ensure_ascii=False, indent=2), encoding="utf-8")

    L = ["# DWG 批量体检总表\n",
         f"目录：`{root}`　主图 {len(rows)} 张　外部参照 {len(xrows)} 个\n",
         "## 一、主图\n",
         "| 文件 | 大小KB | 版本 | 图层 | 参照 | 代理 | 错误 | 警告 | 提示 | 主要问题 | 耗时s |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("fatal"):
            L.append(f"| {r['name']} | {r['kb']} | — | — | — | — | — | — | — | 打不开：{r['fatal']} | — |")
            continue
        L.append(f"| {r['name']} | {r['kb']} | {r['ver']} | {r['layers']} | {r.get('xrefs', 0)} | "
                 f"{r['proxy']} | {r['err']} | {r['warn']} | {r['info']} | {r['top']} | {r['secs']} |")
    tot_e = sum(r.get("err", 0) for r in rows)
    tot_w = sum(r.get("warn", 0) for r in rows)
    L.append(f"\n主图合计：**{tot_e} 错误 / {tot_w} 警告**\n")

    if xrows:
        L += ["## 二、外部参照\n",
              "| 参照文件 | 所属主图 | 大小KB | 错误 | 警告 | 提示 | 清理空文字 | 结果 |",
              "|---|---|---|---|---|---|---|---|"]
        for r in xrows:
            if r.get("fatal"):
                L.append(f"| {r.get('name')} | {r.get('owner')} | — | — | — | — | — | **{r['fatal']}** |")
                continue
            res = "体检"
            if r.get("out"):
                res = "已清理" if (r.get("err_after") is not None and r["err_after"] != r["err"]) or r.get("cleaned") else "无需处理"
            L.append(f"| {r['name']} | {r.get('owner')} | {r['kb']} | {r['err']} | {r['warn']} | "
                     f"{r['info']} | {r['cleaned']} | {res} |")
        xe = sum(r.get("err", 0) for r in xrows)
        xw = sum(r.get("warn", 0) for r in xrows)
        L.append(f"\n参照合计：**{xe} 错误 / {xw} 警告**\n")

    (outdir / "总表.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
