# 共用校核：覆盖范围、来源位置与改字

先确认本次范围。局部核查只检查请求页，优先用已有正文、映射和识别缓存；不重新 OCR，不因本次未用服务的凭据或定期查新偏好缺失而停工。未改正文的只读检查无需生成全书改动报告。

## 页码与定位

`<!--pdf-page.N-->` 只表示原 PDF 物理页；`<!--p.N-->` 表示已核定原书印刷页；`<!--leaf.三上-->` 表示原书叶码。不能把不同口径混称。无可靠原书页码时，明确只具备 PDF 页序。缺页及页码跳变核原件，不自动补成连续页。

- 古籍简化流程用 `line_map.py --page-only`，读显式物理页锚点，没有行坐标，也不支持生成可定位文字层。
- 用户需要逐行定位，或现代出版物的默认完整整理时，`line_map.py --layout <layout.json>|1` 或 `--vl-dir <目录> --writing-mode vertical-rl` 生成行/块/页映射，按报告说明精度和估计位置。
- 正文任何修改（包括标题层级）后映射哈希会过期；重新生成映射，再比较和裁图。不要通过删 hash 字段规避验证。

逐行映射依赖三项输入：原 PDF、底稿的坐标与识别文字、最终 Markdown。MinerU 输入取 `layout.json` / `*_middle.json` 的行框和文字；VL 输入取块框和块文字。只有大段或整栏框时，脚本根据原图墨迹切行/列，再按长度分配文字，这些位置标记为 `estimated`。最后用文字对齐把框关联到 Markdown，不能可靠对齐时退回块或页，不能把推算映射称为逐字精确定位。当前看典第二读者结果用于文字比较，不提供这一步的坐标；映射本身也不能证明 OCR 字认对了。

## 第二读者比对

```powershell
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <映射.json> --vl-dir <VL目录> --pages 21-30 --out <检查目录>
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <映射.json> --kandian "<看典目录>|21" --pages 21-30 --out <检查目录>
```

`--vl-dir`、`--kandian`、`--mineru` 三选一；多家对照用不同报告目录。字形表默认从 `<书>/_数据/config.json` 的 `variantEquivalents` 读取，指定 `--config` 可换配置；显式 `--variant` 覆盖配置表。折叠只用于比对，不因此改正文。看典参数含原 PDF 起页或抽页列表，见 [guji-apis.md](guji-apis.md)。

`_reader_coverage.json` 只按 `--pages` 请求范围统计，区分第二读者缺页和正文未映射页；未完整覆盖返回退出码 2。不能把请求外页面报成必须补跑的范围。一处 1–4 字的分歧进 `doubts.json`，更大分歧交原图检查；分歧不是错误判决，两家一致也不能证明零错误。

每条疑字保留实际 `reader` 和正文 `sourceHash`。字段 `minerU` 是兼容旧格式的“正文现字”，不表示正文一定来自 MinerU；`ocr` 是所选第二读者读法。

## 判定与执行

```powershell
python -X utf8 <skill>/scripts/doubt_sheets.py crops --book <书> --pdf <书>/书.pdf --doubts <检查目录>/doubts.json --out <检查目录>
```

AI 或用户根据原图逐处判断，低把握不硬改。用户授权校订及费用后可按 [ai-judging.md](ai-judging.md) 批量判定；仅比对或截图不自动升级为付费校订。需要用户拍板时把原图发在对话中，不只列字符。

`apply_doubts.py` 要求新版带 `sourceHash` 的 manifest。旧清单重新比对和裁图；不要手工补一个当前 hash 冒充原截图版本。脚本按原始快照累计应用，`apply-state.json` 记录已经执行的判定，同一批重跑跳过已执行项；低把握项后来确认时仍按原始位置应用。正文被外部修改、已执行决定被改写、判定清单换批次时拒绝执行，应从最新正文重新生成新批次。

保留原始响应、原稿快照、backup、apply-state 和报告，不手动删除状态后复用旧偏移。校注与正文分开，执行后以原稿/原生结果运行 `diff_report.py`；更新需要继续使用的映射。原件误印默认照录，OCR 漏读可按原图补回；没有原图依据的不补字、不改卷号。异体字遵从已有本书方针，尚未确定且本次确实需要改变字形时再询问。
