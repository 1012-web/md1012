# 疑字核对：两条工作流共用

主流程第 4 步。只在本次需要文字核对时做；只读比较、截图不自动升级为改正文。批量看图另有费用、尚未获授权时，先按范围估算并说明。

局部核查只查请求页，优先用已有正文、映射和识别缓存；不重新 OCR，不因本次不用的服务凭据缺失而停工。

## 流程

```text
1 定稿快照 → 2 建映射 → 3 比对 → 4 截图 → 5 判定 → 6 执行
      ↑                                              │
      └──── 正文（含标题层级）改动后，从 2 重来 ────────┘
```

### 1. 定稿快照

标题层级、结构先定好，保存为本轮核对的快照。之后任何正文改动都会让映射过期，见 SKILL.md「改正文的先后」。

### 2. 建映射

映射把正文的每段文字对到原 PDF 的页（或行、列）上，供比对与截图定位。

| 方式 | 命令参数 | 用于 |
|---|---|---|
| 页级 | `--page-only` | B 默认。读正文里的 `<!--pdf-page.N-->`，没有行坐标，不能生成文字层 |
| 逐行（MinerU 底稿） | `--layout <layout.json>\|<起页>` | A 默认 |
| 逐行（VL 底稿） | `--vl-dir <VL目录> --writing-mode vertical-rl` | A 用 VL 起稿时 |

```powershell
python -X utf8 <skill>/scripts/line_map.py --pdf <书>/<原件>.pdf --md <书>/01-正文.md --page-only --out <书>/_数据/页级映射
python -X utf8 <skill>/scripts/line_map.py --pdf <书>/<原件>.pdf --md <书>/01-正文.md --layout "<书>/_数据/MinerU/layout.json|1" --out <书>/_数据/逐行映射
```

逐行映射用三样输入：原 PDF、底稿的框与文字、最终 Markdown。底稿只有大段或整栏框时，脚本按墨迹切行/列，再按长度分摊文字，这些位置标为 `estimated`；对不上的退回块或页。所以推算映射不能叫逐字精确定位，映射本身也不证明字认对了。看典结果只用于比字，不提供映射坐标。

映射记录正文哈希。正文一改，映射就过期，必须重建；不要删掉 hash 字段来绕过检查。

### 3. 比对

```powershell
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <映射.json> --vl-dir <书>/_数据/VL --pages 21-30 --out <书>/_数据/疑字判定/b01
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <映射.json> --kandian "<书>/_数据/看典|21" --pages 21-30 --out <书>/_数据/疑字判定/b01
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <映射.json> --mineru "<书>/_数据/MinerU|21" --pages 21-30 --out <书>/_数据/疑字判定/b01
```

- `--vl-dir`、`--kandian`、`--mineru` 三选一；多家交叉核对时各用一个输出目录。看典的 `目录|起页` 写法见 [guji-apis.md](guji-apis.md)。
- 字形表默认读 `<书>/_数据/config.json` 的 `variantEquivalents`；`--config` 换配置，`--variant` 显式覆盖。字形折叠只用于比对，不改正文。
- `--pages` 是本次的原 PDF 页范围。`_reader_coverage.json` 只统计这个范围，分开列出第二读者缺页和正文未映射页；覆盖不全时退出码为 2。范围外的页不算缺口。
- 1–4 字的分歧进 `doubts.json`；更大的分歧单列，交原图核查（可能是编造、漏读或结构问题）。

`doubts.json` 每条的主要字段：

| 字段 | 含义 |
|---|---|
| `current` | 正文现字（2026-10-20 前的旧文件叫 `minerU`，脚本仍能读） |
| `ocr` | 第二读者的读法 |
| `reader` | 实际的第二读者（VL、看典或 MinerU） |
| `sourceHash` | 生成时的正文版本 |
| `file`、`from`、`to` | 所在文件与 UTF-16 偏移 |
| `pdfPage`、`page`、`rect` | 物理页、印刷页、截图框（页级映射时为空） |

### 4. 截图

```powershell
python -X utf8 <skill>/scripts/doubt_sheets.py crops --book <书> --pdf <书>/<原件>.pdf --doubts <批次目录>/doubts.json --out <批次目录>
```

逐行映射截整行/整列，页级映射或无框时截整页。必须用原 PDF，不用页码起点不同的抽页小 PDF。正文版本与疑字不符时脚本拒绝截图。

### 5. 判定

AI 或用户看原图逐条判定，上下文只作辅助：

| 结论 | 含义 |
|---|---|
| A | 正文现字对；原件误印也判 A，另记 `misprint` |
| B | 第二读者对 |
| C | 两家都不对，写出原图实际的字 |
| D | 看不清 |

- 每条给把握"高/低"。低把握不动正文；需要用户拍板时把原图贴进对话，不只列字符。`doubt_sheets.py owner --dir <批次目录> --groups <分组.json>` 可生成给用户看的分组图。
- 少量疑字在当前对话判；量大、已获授权且环境支持子代理时，按 [judging-prompt.md](judging-prompt.md) 分批判。记录实际判定者，不虚构人工核验。
- `merge_judgement.py <批次目录>` 合并各批，检查缺号、重号和字段，再按"原字→改字"分组列出。分组只是方便审阅，每个位置仍要有自己的图像依据，不能看一例就替换全书。

### 6. 执行

```powershell
python -X utf8 <skill>/scripts/apply_doubts.py --book <书> --dir <批次目录> --variants keep
```

- `--variants keep` 保留原书字形；`normalize` 按用户已定的规范写法改，并记原字。
- `--skip` / `--skip-file` 跳过要整体处理的条目（如整页重录、混进正文的注文）。
- 只自动执行把握高的 B、C。原件误印照录，未经许可不改成应有的字；没有原图依据不补字、不改卷号。
- 执行后用 `diff_report.py` 对原稿或原生结果出全文改动报告（`apply-report.json` 只是本批的记录），并重建以后还要用的映射。

## 重跑与版本安全

- 只接受带 `sourceHash` 的新版清单。旧清单要重新比对、截图；不要手工补一个当前 hash 冒充。
- 脚本保留原始快照、`backup/` 和 `apply-state.json`。同一批重跑时跳过已执行项；原来低把握的项后来确认，可在同一批累计执行，仍按原始位置。
- 以下情况脚本停止、不猜新位置：已执行的判定被改写、正文被外部编辑、manifest 换了批次。此时从最新正文重建映射、疑字和截图，放进新批次目录。
- 不要删执行状态后重用旧清单。

## 费用参考

历史批次中，单字拼图判定约每条 1600–2100 tokens，竖排补标点约每段 5200 tokens。这不是当前模型的固定价格；整页图和新模型按实际试样修正。只在真要启动额外付费批次时给估算，不把询价变成局部任务的前置步骤。

## 局限

模型和第二读者都会误读异体、专名和原刊误印。没有足够原图依据时保留 pending。Unicode 未收的字形用部件式（如「⿰口厌」）或原图记录，正文取舍交用户，不背离已选的字形方针。
