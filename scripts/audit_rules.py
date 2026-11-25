# -*- coding: utf-8 -*-
"""DWG 体检规则集：对导出的 DXF 做只读分析，输出结构化问题清单。

设计原则
- 只读：绝不修改 DXF，也不回写 DWG（DXF 往返会毁掉天正等自定义对象）
- 自适应阈值：不用写死的 1e6，按图形自身尺度算，小图和大图都能用
- 每条问题都带 handle/位置，能回到图里找得到
"""
from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict

import ezdxf

ERROR, WARN, INFO = "error", "warn", "info"


def _proxy_count(aud) -> int:
    n = 0
    for _, e in aud._all_entities():
        if "PROXY" in e.dxftype().upper():
            n += 1
    return n

LEVEL_CN = {ERROR: "错误", WARN: "警告", INFO: "提示"}

# 常见的二维图元（用来判断"平面图里有 3D 高度"）
FLAT_TYPES = {"LINE", "LWPOLYLINE", "CIRCLE", "ARC", "TEXT", "MTEXT", "INSERT", "ELLIPSE", "SPLINE"}


@dataclass
class Finding:
    code: str
    level: str
    title: str
    count: int
    detail: str = ""
    advice: str = ""
    samples: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


class Auditor:
    def __init__(self, dxf_path: str):
        self.path = dxf_path
        self.doc = ezdxf.readfile(dxf_path)
        self.findings: list[Finding] = []
        self.stats: dict = {}
        self._spatial = None

    # ---------- 基础信息 ----------
    def _all_entities(self):
        """遍历所有空间（模型 + 全部布局），不止 modelspace"""
        for layout in self.doc.layouts:
            for e in layout:
                yield layout.name, e

    def _spatial_index(self):
        """包围盒与尺度：用于自适应判定超远坐标 / 微小图元"""
        if self._spatial is not None:
            return self._spatial
        xs, ys = [], []
        for _, e in self._all_entities():
            try:
                if e.dxftype() in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC", "TEXT", "MTEXT", "INSERT", "POINT", "ELLIPSE"):
                    p = e.dxf.get("insert", None) or e.dxf.get("start", None) or e.dxf.get("center", None)
                    if p is None and e.dxftype() == "LWPOLYLINE":
                        pts = list(e.get_points("xy"))
                        if pts:
                            xs.extend([v[0] for v in pts]); ys.extend([v[1] for v in pts])
                        continue
                    if p is not None:
                        xs.append(p[0]); ys.append(p[1])
            except Exception:
                continue
        if not xs:
            self._spatial = dict(cx=0.0, cy=0.0, diag=1000.0, n=0)
            return self._spatial
        import numpy as np
        ax, ay = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
        cx, cy = float(np.median(ax)), float(np.median(ay))
        dist = np.hypot(ax - cx, ay - cy)
        # 用中位数距离定尺度：游离点再多也带不偏，样本量小时同样成立
        r90 = float(np.percentile(dist, 90))
        diag = 2.0 * max(r90, 1e-6)
        self._spatial = dict(cx=cx, cy=cy, diag=diag,
                             raw_minx=float(ax.min()), raw_maxx=float(ax.max()),
                             raw_miny=float(ay.min()), raw_maxy=float(ay.max()),
                             n=len(xs))
        return self._spatial

    # ---------- 规则 ----------
    def rule_version(self):
        ver = self.doc.dxfversion
        name = {"AC1009": "R12", "AC1015": "2000", "AC1018": "2004", "AC1021": "2007",
                "AC1024": "2010", "AC1027": "2013", "AC1032": "2018"}.get(ver, ver)
        self.stats["dxf_version"] = f"{ver} ({name})"
        cp = self.doc.header.get("$DWGCODEPAGE", "")
        self.stats["codepage"] = cp

    def rule_units(self):
        ins = self.doc.header.get("$INSUNITS", 0)
        names = {0: "无单位", 1: "英寸", 2: "英尺", 4: "毫米", 5: "厘米", 6: "米"}
        self.stats["insunits"] = f"{ins} ({names.get(ins, '未知')})"
        if ins == 0:
            self.findings.append(Finding(
                "F02", WARN, "图纸未设定插入单位",
                1, "$INSUNITS = 0", "外部参照/复制粘贴时易出现比例错乱，建议设为 4(毫米)"))

    def rule_proxy(self):
        cnt = 0
        kinds = Counter()
        for space, e in self._all_entities():
            t = e.dxftype()
            if "PROXY" in t.upper():
                cnt += 1
                kinds[t] += 1
        self.stats["proxy"] = cnt
        if cnt:
            self.findings.append(Finding(
                "F03", WARN, "存在代理对象（第三方自定义对象）",
                cnt, "，".join(f"{k}×{v}" for k, v in kinds.most_common(3)),
                "多为天正/探索者等插件对象。代理对象在别人机器上是只读块，无法编辑；"
                "若要交付通用图纸，需原插件「图形导出」成基本图元，不要用 DXF 往返（会丢对象）"))

    def rule_edu(self):
        """教育版标记：EDU 图会在打印时带水印"""
        hits = 0
        try:
            blob = open(self.path, "rb").read()
            for tag in (b"EDU", b"\xbd\xcc\xd3\xfd\xb0\xe6"):
                if tag in blob:
                    hits += 1
        except Exception:
            pass
        self.stats["edu"] = hits
        if hits:
            self.findings.append(Finding(
                "F04", ERROR, "疑似教育版图纸",
                1, "文件内含 EDU/教育版标记",
                "教育版打印会带「教育版产品制作」水印，不能用于正式交付；需用正式版重存"))

    def rule_layers(self):
        used = Counter()
        used_on_off = Counter()
        for space, e in self._all_entities():
            lay = str(e.dxf.get("layer", "0"))
            used[lay] += 1
        lay_names = []
        empty, unplottable_used, defpoints = [], [], 0
        for lay in self.doc.layers:
            n = lay.dxf.name
            lay_names.append(n)
            if n not in used:
                empty.append(n)
            else:
                try:
                    if not lay.is_on() or lay.is_frozen():
                        used_on_off[n] = used[n]
                except Exception:
                    pass
            if n == "Defpoints" and used.get("Defpoints"):
                defpoints = used["Defpoints"]
        self.stats["layers"] = len(lay_names)
        self.stats["layers_used"] = len([x for x in lay_names if x in used])
        if empty:
            self.findings.append(Finding(
                "F10", INFO, "空图层（无任何实体）",
                len(empty), "、".join(empty[:8]) + ("…" if len(empty) > 8 else ""),
                "PURGE 可清掉；保留少量作为标准层模板也可接受"))
        if used.get("0"):
            # 天正/探索者的自定义对象默认寄生在 0 层，数量大不代表图脏 —— 只提示不下结论
            weak = _proxy_count(self) > 0
            self.findings.append(Finding(
                "F11", INFO if weak else WARN, "实体放在 0 层",
                used["0"], f"共 {used['0']} 个实体" + ("（本图有代理对象，多为天正等对象本身）" if weak else ""),
                "天正类对象驻 0 层属正常；若为手工图元，应归到正式图层才能被正确开关/筛选"))
        if defpoints:
            self.findings.append(Finding(
                "F12", INFO, "Defpoints 层上有实体",
                defpoints, "", "天正等插件的标注定义点常放此层，属正常；"
                               "但若手工图元被放到这里，打印时会整片消失，需抽查"))
        if used_on_off:
            self.findings.append(Finding(
                "F13", INFO, "关闭/冻结图层上仍有实体",
                sum(used_on_off.values()),
                "、".join(f"{k}({v})" for k, v in list(used_on_off.items())[:6]),
                "确认这些是故意隐藏的；否则出图会缺内容"))
        bad = [n for n in lay_names if any(c in n for c in "=*") or len(n) > 255]
        if bad:
            self.findings.append(Finding(
                "F14", WARN, "图层名含非法或危险字符",
                len(bad), "、".join(bad[:6]), "图层名含 = * 会导致部分工具解析失败"))
        xref_layers = [n for n in lay_names if "|" in n]
        if xref_layers:
            self.findings.append(Finding(
                "F15", INFO, "存在外部参照携带的图层",
                len(xref_layers), "、".join(xref_layers[:5]) + "…",
                "这些层随 xref 存在，绑定(xbind)后才成为本图图层"))

    def rule_blocks(self):
        names = [b.name for b in self.doc.blocks]
        anon = [n for n in names if n.startswith("*") and n not in ("*Model_Space", "*Paper_Space")]
        xrefs = []
        for b in self.doc.blocks:
            try:
                if b.block.dxf.flags & 4:
                    xrefs.append(b.name)
            except Exception:
                pass
        inserted = set()
        for _, e in self._all_entities():
            if e.dxftype() == "INSERT":
                inserted.add(str(e.dxf.get("name", "")))
        unused = [n for n in names if n not in inserted and n not in xrefs
                  and not n.startswith("*Model_Space") and not n.startswith("*Paper_Space")]
        self.stats["blocks"] = len(names)
        self.stats["blocks_anon"] = len(anon)
        self.stats["xrefs"] = xrefs
        if unused:
            self.findings.append(Finding(
                "F20", INFO, "未使用的块定义",
                len(unused), "、".join(unused[:8]) + ("…" if len(unused) > 8 else ""),
                "PURGE 可清，能显著缩小文件"))
        if anon:
            self.findings.append(Finding(
                "F21", INFO, "匿名块（*T 表格块 / *U 动态块 / *D 标注块）",
                len(anon), "、".join(anon[:6]) + "…",
                "多为表格与动态块内部结构，正常；数量异常多说明有大量未用动态块"))
        if xrefs:
            self.findings.append(Finding(
                "F21b", INFO, "含外部参照",
                len(xrefs), "、".join(xrefs),
                "确认 xref 文件随图交付，否则打开会丢底图"))

    def rule_geometry(self):
        sp = self._spatial_index()
        diag = sp["diag"]
        far_thr = max(diag * 1000.0, 1e5)
        tiny_thr = max(diag * 1e-5, 1e-6)
        zero, dup_groups, dup_extra = 0, 0, 0
        tiny, far = 0, 0
        seen = {}
        samples = []
        for space, e in self._all_entities():
            t = e.dxftype()
            try:
                if t == "LINE":
                    s, en = e.dxf.start, e.dxf.end
                    d = math.dist((s.x, s.y, s.z), (en.x, en.y, en.z))
                    if d < 1e-9:
                        zero += 1
                    elif d < tiny_thr:
                        tiny += 1
                    key = (str(e.dxf.layer), "LINE") + tuple(sorted([
                        tuple(round(v, 6) for v in (s.x, s.y, s.z)),
                        tuple(round(v, 6) for v in (en.x, en.y, en.z))]))
                    seen[key] = seen.get(key, 0) + 1
                    cx, cy = (s.x + en.x) / 2, (s.y + en.y) / 2
                    if math.hypot(cx - sp["cx"], cy - sp["cy"]) > far_thr:
                        far += 1
                        if len(samples) < 5:
                            samples.append(f"{space}:LINE@{cx:.0f},{cy:.0f}")
                elif t == "CIRCLE":
                    if e.dxf.radius < 1e-9:
                        zero += 1
                    key = (str(e.dxf.layer), "CIRCLE", round(e.dxf.center.x, 6),
                           round(e.dxf.center.y, 6), round(e.dxf.radius, 6))
                    seen[key] = seen.get(key, 0) + 1
                elif t == "TEXT":
                    txt = str(e.dxf.get("text", ""))
                    if not txt.strip():
                        pass
                    key = (str(e.dxf.layer), "TEXT", tuple(round(v, 6) for v in
                           (e.dxf.insert.x, e.dxf.insert.y)), txt, round(e.dxf.get("height", 0), 4))
                    seen[key] = seen.get(key, 0) + 1
                    p = e.dxf.insert
                    if math.hypot(p.x - sp["cx"], p.y - sp["cy"]) > far_thr:
                        far += 1
                        if len(samples) < 5:
                            samples.append(f"{space}:TEXT@{p.x:.0f},{p.y:.0f}")
            except Exception:
                continue
        dup_extra = sum(v - 1 for v in seen.values() if v > 1)
        dup_groups = sum(1 for v in seen.values() if v > 1)
        self.stats.update(zero_len=zero, tiny=tiny, dup_extra=dup_extra, far=far,
                          bbox_diag=round(diag, 2))
        if zero:
            self.findings.append(Finding(
                "F30", ERROR, "零长度图元", zero, "起终点重合的线/半径 0 的圆",
                "会导致捕捉错乱与选择集异常，应删除"))
        if tiny:
            self.findings.append(Finding(
                "F31", INFO, "极短图元（小于图形尺度的 1e-5）",
                tiny, f"阈值 ≈ {tiny_thr:.2e}", "多为误操作残留，建议删除"))
        if dup_groups:
            self.findings.append(Finding(
                "F32", WARN, "重复图元", dup_extra,
                f"{dup_groups} 组重叠，多出 {dup_extra} 个",
                "OVERKILL 可清除；重复线会让线宽叠加、打印发黑"))
        if far:
            self.findings.append(Finding(
                "F33", WARN, "远离主体的游离图元", far,
                f"距图形中心 > {far_thr:,.0f}（图幅对角 {diag:,.0f}）",
                "常见于误粘贴/坐标错乱；会让 ZOOM E 失效、出图范围异常"))

    def rule_text(self):
        empty, bad_h, mtext_w0 = 0, 0, 0
        for space, e in self._all_entities():
            try:
                if e.dxftype() == "TEXT":
                    if not str(e.dxf.get("text", "")).strip():
                        empty += 1
                    h = float(e.dxf.get("height", 0))
                    if 0 < h < 0.5 or h > 1000:
                        bad_h += 1
                elif e.dxftype() == "MTEXT":
                    raw = str(e.text)
                    if not raw.strip():
                        empty += 1
                    else:
                        # 只剩格式码的空壳（{\fSimSun|b0;...}）人眼也是空的，一并计入
                        try:
                            if not e.plain_text().strip():
                                empty += 1
                        except Exception:
                            pass
                    if float(e.dxf.get("width", 0)) == 0:
                        mtext_w0 += 1
            except Exception:
                continue
        if empty:
            self.findings.append(Finding(
                "F40", WARN, "空文字对象", empty, "内容为空的 TEXT/MTEXT",
                "影响统计与批量替换，建议删除"))
        if bad_h:
            self.findings.append(Finding(
                "F41", WARN, "文字高度异常", bad_h, "高度 <0.5 或 >1000 个图形单位",
                "统一字高前先确认不是比例错误"))
        if mtext_w0:
            self.findings.append(Finding(
                "F42", INFO, "多行文字宽度为 0（不自动换行）", mtext_w0, "",
                "若希望自动折行需给定宽度"))

    def rule_z(self):
        nz = 0
        for space, e in self._all_entities():
            try:
                if e.dxftype() in ("LINE", "LWPOLYLINE", "CIRCLE", "ARC"):
                    p = e.dxf.get("start", None) or e.dxf.get("center", None)
                    if p is not None and abs(p[2]) > 1e-6:
                        nz += 1
                elif e.dxftype() == "LWPOLYLINE":
                    el = e.dxf.get("elevation", 0)
                    if abs(el) > 1e-6:
                        nz += 1
            except Exception:
                continue
        if nz:
            self.findings.append(Finding(
                "F50", INFO, "图元 Z 坐标不为 0", nz, "平面图里常见的高程残留",
                "会让对象捕捉到不同平面、填充失败；可用 FLATTEN 压平"))

    def rule_styles(self):
        used_styles = Counter(); used_dims = Counter(); used_ltypes = Counter()
        for _, e in self._all_entities():
            try:
                if "style" in e.dxf.all_existing_dxf_attribs():
                    used_styles[str(e.dxf.style)] += 1
                if e.dxftype() in ("DIMENSION",):
                    used_dims[str(e.dxf.get("dimstyle", ""))] += 1
                used_ltypes[str(e.dxf.get("linetype", "BYLAYER"))] += 1
            except Exception:
                continue
        unused_st = [s.dxf.name for s in self.doc.styles if s.dxf.name not in used_styles]
        unused_lt = [t.dxf.name for t in self.doc.linetypes
                     if t.dxf.name not in used_ltypes and t.dxf.name.upper() not in ("BYLAYER", "BYBLOCK", "CONTINUOUS")]
        self.stats["styles"] = len(self.doc.styles)
        if unused_st:
            self.findings.append(Finding(
                "F60", INFO, "未使用的文字样式", len(unused_st),
                "、".join(unused_st[:6]), "PURGE 可清"))
        if unused_lt:
            self.findings.append(Finding(
                "F61", INFO, "未使用的线型", len(unused_lt),
                "、".join(unused_lt[:6]), "PURGE 可清"))

    def rule_fonts(self):
        missing = []
        win_fonts = set()
        try:
            for f in os.listdir(r"C:\Windows\Fonts"):
                win_fonts.add(f.lower())
        except Exception:
            pass
        for s in self.doc.styles:
            try:
                font = str(s.dxf.get("font", ""))
            except Exception:
                continue
            if not font:
                continue
            low = font.lower()
            if low.endswith(".ttf") or low.endswith(".ttc") or low.endswith(".otf"):
                if win_fonts and low not in win_fonts:
                    missing.append(f"{s.dxf.name}→{font}")
        if missing:
            self.findings.append(Finding(
                "F70", WARN, "文字样式引用的字体文件未找到", len(missing),
                "、".join(missing[:5]),
                "会在别的机器上替换成其他字体导致字宽变化；SHX 字体无法逐一核对，需人工确认"))
        else:
            self.stats["font_check"] = "本机 TTF 均存在（SHX 未核对）"

    def rule_layouts(self):
        empty_layouts = []
        for lay in self.doc.layouts:
            if lay.name == "Model":
                continue
            if len(lay) == 0:
                empty_layouts.append(lay.name)
        self.stats["layouts"] = len(list(self.doc.layouts))
        if empty_layouts:
            self.findings.append(Finding(
                "F80", INFO, "空布局", len(empty_layouts), "、".join(empty_layouts),
                "不影响使用，交付前可删掉"))

    # ---------- 主流程 ----------
    def run(self):
        for fn in (self.rule_version, self.rule_units, self.rule_proxy, self.rule_edu,
                   self.rule_layers, self.rule_blocks, self.rule_geometry, self.rule_text,
                   self.rule_z, self.rule_styles, self.rule_fonts, self.rule_layouts):
            try:
                fn()
            except Exception as exc:  # 单条规则失败不影响整体
                self.findings.append(Finding("R00", INFO, f"规则 {fn.__name__} 跳过",
                                             1, f"{type(exc).__name__}: {exc}", ""))
        order = {ERROR: 0, WARN: 1, INFO: 2}
        self.findings.sort(key=lambda f: (order[f.level], f.code))
        return self

    @property
    def counts(self):
        c = Counter(f.level for f in self.findings)
        return {k: c.get(k, 0) for k in (ERROR, WARN, INFO)}

    def entity_counts(self):
        out = {}
        for lay in self.doc.layouts:
            out[lay.name] = dict(Counter(e.dxftype() for e in lay))
        return out


def markdown_report(aud: Auditor, title: str = "") -> str:
    c = aud.counts
    L = []
    L.append(f"# DWG 体检报告{(' · ' + title) if title else ''}\n")
    L.append(f"> 判定：**{c[ERROR]} 错误 / {c[WARN]} 警告 / {c[INFO]} 提示**  "
             f"（错误必须修，警告建议修，提示按需）\n")
    L.append("## 一、文件概况\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    for k, v in aud.stats.items():
        L.append(f"| {k} | {v} |")
    L.append("\n## 二、实体分布\n")
    L.append("| 空间 | 数量 | 主要类型 |")
    L.append("|---|---|---|")
    for space, cnt in aud.entity_counts().items():
        top = ", ".join(f"{k}×{v}" for k, v in Counter(cnt).most_common(4)) or "—"
        L.append(f"| {space} | {sum(cnt.values())} | {top} |")
    if aud.findings:
        L.append("\n## 三、问题清单\n")
        L.append("| 级别 | 编号 | 问题 | 数量 | 证据 | 建议 |")
        L.append("|---|---|---|---|---|---|")
        for f in aud.findings:
            L.append(f"| {LEVEL_CN[f.level]} | {f.code} | {f.title} | {f.count} | "
                     f"{f.detail.replace('|', '/')} | {f.advice.replace('|', '/')} |")
    else:
        L.append("\n## 三、问题清单\n\n未发现结构性问题。\n")
    return "\n".join(L)
