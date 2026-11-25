---
name: cad-dwg-doctor
description: 检查、修复和清理 Windows AutoCAD DWG 图纸，生成 DXF 只读体检及外部参照报告；在用户要求检查教育版打印戳记时提供打印比对。适用于 DWG 报错、图纸瘦身、零长线及交付前检查，不处理设计修改或纯 PDF 编辑。
---

# CAD 图纸医生

对 DWG 副本调用本机 AutoCAD `accoreconsole.exe`；用导出的 DXF 做只读分析。所有脚本路径相对本技能目录，执行时使用技能的绝对路径。需要 Windows、Python 3.10+ 和已安装并激活的 AutoCAD。依赖见 `requirements.txt`，先检查当前环境，按需要安装；不要替用户安装或激活 AutoCAD。

## 选择工作流

- 用户只要求分析、检查、体检时运行 `audit`。
- 用户要求修复时运行 `repair`；要求修复并清理时运行 `all`。两者目前均执行 AUDIT/PURGE，默认删除零长线和空文字；`--no-clean` 保留这些实体。
- 教育版打印戳记检查使用 `eduprint.py check`。只有用户明确要求清戳且具备相应文件处理权限时使用 `edustrip`；需要 AutoCAD 2022+。去戳不改变文件的许可资格。
- 不自动删除重复图元、游离图元或修改设计几何。重复图元只报告，人工决定是否在 GUI 运行 OVERKILL。

## 执行与验收

1. 确定输入及独立输出目录。检查 `python --version`、依赖和引擎路径；可通过 `--acc` 或环境变量 `ACCORECONSOLE` 指定引擎。先用 `--help` 确认当前参数。
2. 运行对应命令，保留退出码和报告：

```powershell
python <技能目录>/scripts/dwgpipe.py audit "图纸.dwg" --out "报告目录"
python <技能目录>/scripts/dwgpipe.py all "图纸.dwg" --out "修复目录"
python <技能目录>/scripts/batch_check.py "图纸目录" --out "批量报告目录"
python <技能目录>/scripts/eduprint.py check "图纸.dwg" --out "打印报告目录"
```

3. 原图及参照始终保留；AUDIT/PURGE 仅作用于临时副本。常规修复按原 DWG 版本另存，核对文件头并分析修后 DXF。DXF 往返不用于正式修复，以免损失天正等自定义对象。代理对象保留情况仍需原宿主验证。
4. 查看 JSON 的 `fatal`、参照缺失、修复前后问题数量和日志。退出码 0 表示处理流程成功，不表示图纸没有错误或达到专业交付标准。缺参照、超出递归深度、绝对参照路径或目录外参照都应说明，不能据目录存在就保证整包可移植。
5. 报告输入、成品、检查范围、剩余问题及未验证部分。文件大小变化仅作参考；有成品也不代表打印和设计正确。

## 外部参照

默认跟随直接参照；`--xref-depth 2` 扩展一层，`--no-xref` 只处理主图。批量串行，共用参照缓存。修复整包保留原相对布局，但不重写 DWG 内嵌绝对路径。交付前须在隔离目录重开整包并确认所有参照解析到包内；未验证时明确称为候选包。

## 打印戳记的边界

打印法仅分析当前输出页面的边条形态，是样本校准的启发式，会受布局、纸张、内容遮挡和内核授权影响。未命中应称“本次打印未检出”，不能证明文件无 EDU 标记或所有布局无水印。用已知对照图确认打印内核不会自行加戳，保留 PDF/PNG 并人工复核。版本年份不能证明商业授权。

`edustrip` 默认前后打印；前置判定失败、会话失败、未保存或复验失败时不发布成品。`--verify none` 产物未经验证，不能作为去戳成功证据。`educheck` 的 ENTER 提示、字节扫描、`deedu` 的 DXF 往返均为废弃判据，不用于正式结论。

## 按需参考

- 了解各体检项及严重度：读 [references/checklist.md](references/checklist.md)。
- 高级参数、参照、日志及历史现场记录：读 [references/operations.md](references/operations.md)。其中历史实测不是本版本验证证据；与入口冲突时按入口的能力边界执行。
