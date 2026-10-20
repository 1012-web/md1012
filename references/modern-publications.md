# 工作流 A：论文和现代出版物

适用材料、验收标准和共用规则见 [SKILL.md](../SKILL.md)。本页只写 A 特有的操作，步骤编号与主流程一致。只做局部任务时按用户范围验收，不要求重新解析全书。

## 1. 预检

本地预检（`pdf_preflight.py`），抽看正文、注区、目录、图表和特殊页。现代竖排本先检查 MinerU 的结构分类，不把"有坐标"当成标题、脚注都已分对。

## 2. 起稿：恢复 MinerU 原生结构

1. 常规用 MinerU 作结构底稿、VL 作第二读者；已有缓存直接用。上传与凭据见 [mineru-api.md](mineru-api.md)、[vl.md](vl.md)。
2. 从 `layout.json` / `*_middle.json` 提取正文和原生结构，`discarded_blocks` 里的脚注等一并检查。`full.md` 只作对照，不作正文来源。字段说明见 [mineru-outputs.md](mineru-outputs.md)。
3. `probe.py` 列出脚注圈码、跨页断段、目录缺题等候选，核原图处理；`build.py` 忠实合并已有信息，不用另一套猜测替换可用的脚注与表格。

```powershell
python -X utf8 <skill>/scripts/probe.py <书>/_数据/MinerU
python -X utf8 <skill>/scripts/build.py <书>/_数据/MinerU -o <书>/_数据/过程报告/_skeleton.md
```

- 分卷骨架用 `merge_skeletons.py` 按原书顺序衔接，检查接缝和脚注编号。
- 表格保留 HTML 行列结构及原图；图片用 `copy_images.py` 只复制被引用的，不删原始 ZIP 或结果。

**MinerU 部分失效时**：文字差但框架可用，用 VL 协助识字；结构也失效，用按页 VL 稿起步，再按出版物实际的脚注和结构核查。这本书仍属 A，不改用 B 的验收。改用 VL 起稿时保留整理前快照和原始 VL 响应。

## 3. 目录与多级标题

按 [toc-headings.md](toc-headings.md) 执行。没有目录的论文依据正文明确的标题形式，注明依据，不强造目录或更多层级。

## 4. 疑字核对

按 [doubt-check.md](doubt-check.md) 执行，A 的默认选择：

- 映射用逐行（`line_map.py --layout` 或 `--vl-dir`）；文字层 PDF 只在用户需要时生成。
- 第二读者通常是 VL。看原图区分 OCR 错读与出版物本身误印。
- 标点缺失先看原图：原书本来没有就不补；文字层映射损坏才读图恢复或重新 OCR。脚注不按模型语义补标点。

## 5. 结构核查

- 脚注：核对正文注号、注文定义和原注区三者对应。
- 标题、注、图表、正文互相混淆时定点修复，不只核文字数量。
- 需要逐页核注区、补篇题、重录清单页、清残留时，按 [structure-audit.md](structure-audit.md) 只做本次需要的项目。

## 6. 交付

`split_book.py` 把成品整理为正文一份、卷首书末另存；用户明确要求时才按章拆分。论文用 `--level 2`，书目信息用 `--meta` 明确给出。检查 `_split_report.txt` 的去向和页码，避免序论等被误归卷首。

```powershell
python -X utf8 <skill>/scripts/verify.py <书>/01-正文.md
python -X utf8 <skill>/scripts/diff_report.py <书>/_数据/MinerU <书>/01-正文.md
python -X utf8 <skill>/scripts/diff_report.py --before <原稿.md> <书>/01-正文.md
```

第二条用于 MinerU 起稿；第三条用于 VL 起稿或其他有整理前快照的情况，比较原始稿而不是已过滤过的正文。报告除 SKILL.md 列的共同项外，另列脚注、图表和结构的核查结果。
