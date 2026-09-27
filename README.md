# md1012（原名 mineru）

通过 [MinerU](https://github.com/opendatalab/MinerU) 精准解析 API 获取 PDF 解析产物，再整理成高质量 Markdown（Obsidian 格式，也适合喂给 AI）的 Claude Code skill。

## 解决什么问题

MinerU 会输出一份 `full.md`，但它把页眉、页码、**脚注**标记为 `is_discarded` 后直接丢弃。实测：

| 文献 | full.md 丢失的脚注 |
|---|---|
| 一篇 12 页期刊论文 | 60 条 |
| 一本 200 页专著 | 305 条 |

对学术文献来说，脚注往往是最有价值的部分。这些内容完整保存在 `layout.json` 的 `discarded_blocks` 里，只是没被渲染出来。

除此之外还处理：跨页段落合并、页码锚点、圈码脚注的全局重编号、图表与图注、标题层级重建，以及某些 PDF 文字层编码损坏导致的**中文标点大面积丢失**。

## 用法

在 Claude Code 中，指向一个 MinerU 输出目录并说明需求即可，skill 会自动触发。手动跑脚本：

```bash
python scripts/mineru_api.py configure                    # 首次安全保存 API Token
python scripts/mineru_api.py submit a.pdf -o <书>/_数据/MinerU原始   # 上传、轮询、下载并解压
python scripts/mineru_api.py usage                        # 本机估算的当日额度
python scripts/probe.py <mineru输出目录>              # 探查 + 六项自动检查
python scripts/build.py <目录> -o <书>/_数据/过程报告/_skeleton.md   # 重建骨架
python scripts/split_book.py <骨架.md> -o <书> --meta book=书名      # 正文合成一份，卷首书末另存，出索引
python scripts/verify.py <成品.md>                    # 结构自检
python scripts/diff_report.py <目录> <成品.md>        # 交付前逐字符改动比对
python scripts/crop_pdf.py <origin.pdf> --page 2 --rect 65,545,300,559 -o crop.png
```

一本书的所有生成文件都放在它自己的文件夹里（正文、索引、校注、图片在顶层，`_数据/` 放 MinerU 原始产物、过程报告与逐字映射，`校勘/` 放校勘报告），打包文件夹即可交给别人。

脚本把报告写成 UTF-8 文件、stdout 只打印 ASCII 状态行——Windows 控制台是 GBK，中文直接 print 会乱码。

精准 API 单文件限制为 200 MB、200 页。`mineru_api.py` 会保留原 PDF，以页面对象复制方式无损拆分超限文件，并在输出目录的 `_upload_cache/` 按来源指纹复用分卷；完成后报告当日本机追踪的解析文件数、页数和估算剩余额度。官方当前定义是每日解析文件总数由基础额度与额外申请额度相加（默认 5000 份），每日 1000 页高优先级；超出高优先级页数后转入普通队列。官方未提供用量查询端点，因此剩余量明确标为本机估算。

## 三条红线

1. **不给正文补字。** 学术文献以原刊为准；疑似脱字只标注，经用户许可才改。
2. **改动原文前先核 PDF 原图。** 分不清是原刊印错还是 OCR 认错，就没资格决定改不改。
3. **改动报告必须由 `diff_report.py` 生成，不能靠回忆。** 曾有一次标点规范化脚本的正则 bug 静默删掉约 85 处括号内容，全靠它揪出来。

## 结构

```
SKILL.md                      主流程
scripts/
  mineru_api.py               精准 API 配置、上传、轮询、下载、拆分与用量报告
  probe.py                    探查、圈码↔脚注对齐审计、六项自动检查
  build.py                    从 layout.json 重建骨架
  verify.py                   结构自检
  diff_report.py              交付前改动比对
  crop_pdf.py                 裁图核对 / 剥离文字层供重跑 OCR
  pdf_sync.py、sync_mapper.py 逐字映射（本书字形表、推定坐标、竖排补行框）
  textlayer_spec.py           从逐字映射导出文字层清单（横排竖排都行）
  pdf_textlayer.py            按清单重建 PDF 隐形文字层（竖排整列一行）
  vl_parse.py                 PaddleOCR-VL 逐页再读（本机 llama.cpp，A 卡走 Vulkan）
  vl_supplement.py            VL 找到而 MinerU 漏掉的块 → 补充行框
  vl_compare.py               正文与 VL 逐页比对，出疑字线索
  vl_build.py                 VL 作底稿时整理 Markdown（推断页码、按分隔线认注、拦编造内容）
  structure_audit.py          结构核查：注区核对材料、目录对篇题、按页任务、按页改动执行（定位串须唯一）、注号重编
  note_tasks.py               读丢的注号（<!--?fnref n-->）按页出任务、放回
  doubt_sheets.py             不经插件：按映射截疑字原图（横排整行、竖排整列）、拼图、给站长的分组拼图
  apply_doubts.py             不经插件：把 AI 判定结果按偏移写回正文（从后往前、核对原字、备份）
  doubt_crops.py、apply_judgement.js 等   经笔记1012 插件的疑字判定（书在 Obsidian 库里时）
references/
  mineru-api.md               精准 API 限制、端点、配置与报告语义
  mineru-outputs.md           六个产物文件各自有什么、缺什么
  templates.md                frontmatter / AI 校注 / 改动报告 模板
  character-sync.md           逐字映射与文字层
  vl.md                       PaddleOCR-VL 第三读者：安装、显卡、实测、竖排古籍流程
  structure-audit.md          底稿的结构核查：顺序与理由、结果格式、实测的坑
  structure-prompts.md        结构核查各步的子代理提示词模板（派子代理时才读）
  ai-judging.md、judging-prompt.md   AI 看原图判定疑字（经插件与不经插件两条路）
tests/
  test_mineru_api.py          无外部调用的 API 上传/轮询/下载端到端测试
```

## 状态

在一篇期刊论文和一本 200 页专著上做过端到端验证（含无 skill 对照组）。竖排标点古籍（扫描件、强制 OCR）《黄道周集》497 页整本走通：VL 底稿 → 逐字映射 → AI 看原图判定疑字 → 结构核查（注、篇题、清单页、残留）→ 竖排文字层。**尚未覆盖**：表格密集型 PDF、双栏排版、英文文献、含公式的理工科文献。
