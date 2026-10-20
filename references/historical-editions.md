# 工作流 B：非现代排印古籍

适用材料、验收标准和共用规则见 [SKILL.md](../SKILL.md)。本页只写 B 特有的操作，步骤编号与主流程一致。

B 的默认验收是**页码、多级标题、识字准确**。原有夹注和附注的文字要保留，但默认不恢复复杂脚注链接、字号行款、逐字坐标或 PDF 文字层；用户另外要求时再扩展。已有试跑只覆盖少量刻本和抄本，不能代表全部刻本或抄本。

## 1. 预检

抽看目录、正文、夹注和难字页；手写本和特殊行款先抽样，比较各家识字效果再定文字来源。

## 2. 起稿：按页原稿

常用 VL 作初稿、看典作第二读者；接口见 [vl.md](vl.md)、[guji-apis.md](guji-apis.md)。已有 MinerU 结果时，它的位置框和读法可以辅助，但不因字读错就全部弃用，也不自动信任它的标题/注区分类。没有可用第二读者时，在报告中注明覆盖缺口。

保持逐页次序生成按页原稿，每页带 `<!--pdf-page.N-->`；页码、叶码标记按 SKILL.md「页码口径」。

```powershell
python -X utf8 <skill>/scripts/vl_build.py build --vl-dir <书>/_数据/VL --pdf <书>/<原件>.pdf --segment 1-30:01-原稿.md --writing-mode vertical-rl --out-dir <书>/_数据/过程报告/按页原稿
```

- `vl_build.py` 默认保守：疑似异常文字和待核分类都保留，不自动改卷号；没有已核的印刷页报告时只写物理页锚点。
- 待核书眉、疑似编造等对照原图定点处理，保留正文原字。`<!--?structure-->`、`<!--?vl-suspect-->`、`<!--?volume-->` 表示仍待核，不是完成标记。
- `--legacy-heuristics`（分隔线判脚注、字号判夹注等旧启发式）不是默认步骤，见 [vl.md](vl.md)。
- 逐页手工整理更简单时，可直接用 VL 的 JSON/TXT 和原图写按页 Markdown，不必走完整重建器。

## 3. 目录与多级标题

按 [toc-headings.md](toc-headings.md) 执行，B 的注意点：

- 以原书卷、篇、门类的实际隶属关系为准，不套通用的"章/节"。
- 不把正文第一个短行默认升成标题；卷号重复时不自动改成下一卷。
- 目录只列到卷或篇时，更细的小题从正文原图识别并注明依据。

## 4. 疑字核对：页级即可

按 [doubt-check.md](doubt-check.md) 执行，B 的默认选择：

- 标题核定后的稿存成新快照，用 `line_map.py --page-only` 建页级映射；不需要逐行框，疑字截整页。
- 第二读者通常是看典。

```powershell
python -X utf8 <skill>/scripts/line_map.py --pdf <书>/<原件>.pdf --md <书>/01-正文.md --page-only --writing-mode vertical-rl --out <书>/_数据/页级映射
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <书>/_数据/页级映射/line-map.json --kandian "<书>/_数据/看典|1" --pages 1-30 --out <书>/_数据/疑字判定/b01
```

## 5. 结构核查

默认不做。用户要求恢复注区、书眉、字号行款时，先抽样验证再按 [structure-audit.md](structure-audit.md) 做所需项目。

## 6. 交付

交付正文 Markdown、目录标题树及核对报告、必要原图、校注和未判疑字。报告除 SKILL.md 列的共同项外，另说明原件版本，以及页码/叶码与 PDF 页序的对应。

```powershell
python -X utf8 <skill>/scripts/diff_report.py --before <书>/_数据/过程报告/按页原稿/01-原稿.md <书>/01-正文.md
```

标题层级变化另在目录报告中列明。图片、缺字框和难以转录的内容保留原图定位，不直接丢掉。
