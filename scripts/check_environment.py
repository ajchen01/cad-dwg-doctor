"""Read-only readiness check. Does not start AutoCAD or open drawings."""
import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

from dwgpipe import discover_acc


DEPENDENCIES = (
    ("ezdxf", "ezdxf", (1, 0), False),
    ("pymupdf", "fitz", (1, 23), True),
    ("opencv-python-headless", "cv2", (4, 8), True),
    ("pillow", "PIL", (10, 0), True),
    ("numpy", "numpy", (1, 24), True),
)


def check(mode="basic", acc=None):
    checks = []

    def add(name, ok, detail, next_step=""):
        checks.append(dict(name=name, ok=bool(ok), detail=detail,
                           next_step="" if ok else next_step))

    add("操作系统", platform.system() == "Windows", platform.system(), "请在安装了 AutoCAD 的 Windows 电脑上使用。")
    add("Python", sys.version_info >= (3, 10), platform.python_version(), "请使用 Python 3.10 或更高版本运行本技能。")
    root = Path(__file__).resolve().parents[1]
    install = '请让助手使用当前 Python 安装技能目录中的 requirements.txt，然后重新检查。'
    for package, module, minimum, printing in DEPENDENCIES:
        if printing and mode == "basic":
            continue
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
            parts = tuple(int(p) for p in version.split(".")[:2])
            add(package, parts >= minimum, version, install)
        except Exception as exc:
            add(package, False, str(exc), install)
    engines = discover_acc()
    if acc:
        path = Path(acc)
        # Explicit paths are accepted, but the year cannot be inferred reliably.
        engines = [(year, p) for year, p in engines if Path(p) == path]
        if path.is_file() and not engines:
            engines = [(0, str(path))]
    usable = [(y, p) for y, p in engines if mode != "strip" or y >= 2022]
    add("AutoCAD 引擎", bool(usable), engines,
        "请提供 AutoCAD 安装位置；清戳需确认 AutoCAD 2022 或更高版本。不会自动安装或激活 AutoCAD。")
    add("技能文件", all((root / p).is_file() for p in
        ("SKILL.md", "requirements.txt", "scripts/dwgpipe.py", "scripts/eduprint.py", "scripts/safe_overwrite.py", "scripts/xref_graph.py", "scripts/edu_workflow.py", "scripts/audit_rules.py")),
        str(root), "请重新解压完整技能包，不要只复制 SKILL.md。")
    return dict(ready=all(c["ok"] for c in checks), mode=mode,
                python=sys.executable, checks=checks,
                limitations="仅检查运行前提，未启动 AutoCAD；激活状态、宿主可访问性、参照完整性和图纸处理效果须在实际任务中验证。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("basic", "print", "strip"), default="basic")
    parser.add_argument("--acc", help="AutoCAD accoreconsole.exe 路径")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(args.mode, args.acc)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("运行前提检查通过，可以开始图纸任务。" if result["ready"] else "暂时还不能开始，请处理以下未通过项。")
        for item in result["checks"]:
            print(("[通过] " if item["ok"] else "[待处理] ") + item["name"] + "：" + str(item["detail"]))
            if item["next_step"]:
                print("  下一步：" + item["next_step"])
        print(result["limitations"])
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
