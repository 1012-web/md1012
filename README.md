# md1012

一套将 PDF（教材、论文、专著、竖排繁体古籍扫描件）转为史学研究可用的可信、高质量 Markdown 的工作流（Claude Code skill），产出 Obsidian 笔记，也适合喂给 AI。

- **可信**：忠于原书，改字须有 PDF 原图为据并另出校注，改动报告由脚本逐字比对生成。
- **高质量**：带脚注、多级标题、页码、图表、标点等信息。
- **对得回原书**：正文每一行对到 PDF 页面上那一行的位置（竖排是那一列），可选生成带隐形文字层的 PDF。

底稿通过 [MinerU](https://github.com/opendatalab/MinerU) 精准解析 API 取得，同时用 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 官方 API 的 PaddleOCR-VL 再读一遍作第二位读者；竖排古籍只用 PaddleOCR-VL。两家都走云端 API，本机不用装模型。**PDF 会上传到 MinerU 与百度的服务器**，未刊稿、有版权或保密顾虑的材料请改用 MinerU 桌面软件手动解析。

## 解决什么问题

MinerU 会输出一份 `full.md`，但它把页眉、页码、**脚注**标记为 `is_discarded` 后直接丢弃。实测：

| 文献 | full.md 丢失的脚注 |
|---|---|
| 一篇 12 页期刊论文 | 60 条 |
| 一本 200 页专著 | 305 条 |

对学术文献来说，脚注往往是最有价值的部分。这些内容完整保存在 `layout.json` 的 `discarded_blocks` 里，只是没被渲染出来。

除此之外还处理：跨页段落合并、页码锚点、圈码脚注的全局重编号、图表与图注、标题层级重建，以及某些 PDF 文字层编码损坏导致的**中文标点大面积丢失**。

## 用法

在 Claude Code 中，给一个 PDF 或 MinerU 输出目录并说明需求即可，skill 会自动触发；首次使用会问几件事（怎么取得解析结果、要不要同时用 PaddleOCR-VL、成品放哪、要不要定期查新模型）。手动跑脚本：

```bash
python scripts/mineru_api.py configure                    # 首次安全保存 MinerU Token
python scripts/paddle_api.py configure                    # 首次安全保存 PaddleOCR 访问令牌
python scripts/mineru_api.py submit a.pdf -o <书>/_数据/MinerU原始   # 上传、轮询、下载并解压
python scripts/paddle_api.py submit a.pdf -o <书>/_数据/VL           # PaddleOCR-VL 逐页结果
python scripts/probe.py <mineru输出目录>              # 探查 + 七类自动检查
python scripts/build.py <目录> -o <书>/_数据/过程报告/_skeleton.md   # 重建骨架
python scripts/split_book.py <骨架.md> -o <书> --meta book=书名      # 正文合成一份，卷首书末另存，出索引
python scripts/verify.py <成品.md>                    # 结构自检
python scripts/diff_report.py <目录> <成品.md>        # 交付前逐字符改动比对
python scripts/line_map.py --pdf a.pdf --md <成品.md> --layout <目录>/layout.json|1 --out <书>/_数据/逐行映射
python scripts/crop_pdf.py <origin.pdf> --page 2 --rect 65,545,300,559 -o crop.png
```

一本书的所有生成文件都放在它自己的文件夹里（正文、索引、校注、图片在顶层，`_数据/` 放两家的原始结果、逐行映射与过程报告），打包文件夹即可交给别人。

脚本把报告写成 UTF-8 文件、stdout 只打印 ASCII 状态行——Windows 控制台是 GBK，中文直接 print 会乱码。

免费额度（2026-10-16 核对）：MinerU 每日 1000 页最高优先级，超出的排普通队列、不拒绝；PaddleOCR 每个模型每日 20000 页。MinerU 精准 API 单文件限制为 200 MB、200 页。`mineru_api.py` 会保留原 PDF，以页面对象复制方式无损拆分超限文件，并在输出目录的 `_upload_cache/` 按来源指纹复用分卷；完成后报告当日本机追踪的解析文件数、页数和估算剩余额度。官方未提供用量查询端点，因此剩余量明确标为本机估算。`paddle_api.py` 按 100 页一块分批提交，中断后重跑即续。

## 对到 PDF 的位置：逐行映射

`line_map.py` 不装模型、不上传：行框来自底稿本身，MinerU 把一段记成一行的、PaddleOCR-VL 只给块框的，都在框内按墨迹投影切成行（竖排切成列），文字按各行长度分摊。对不上行的退到逐块（目录页、块框互相重叠的），再对不上的按页码锚点退到逐页。实测：横排《世界史·古代史编（上卷）》覆盖 99.98%，映射上的字 97% 落在自己那一行；竖排《黄道周集》覆盖 95%，92% 落在自己那一列。

结果是 `line-map.json`（正文的每一段 ↔ PDF 第几页、页上哪个框）。要在 PDF 阅读器、Zotero 里搜索选中时，加 `--spec` 再用 `pdf_textlayer.py build` 生成带隐形文字层的 PDF；原 PDF 不动。

## 疑字判定

两家同时解析的横排书，`vl_compare.py` 拿 PaddleOCR-VL 和成品正文逐页比，读法不同的字就是疑字；`doubt_sheets.py` 截整行原图拼图，交 AI 子代理看原图判定，把握低的发图给用户拍板，`apply_doubts.py` 按偏移写回正文（从后往前、核对原字、备份）。

## 红线

1. **不给正文补字。** 学术文献以原刊为准；疑似脱字只标注，经用户许可才改。
2. **改动原文前先核 PDF 原图。** 分不清是原刊印错还是 OCR 认错，就没资格决定改不改。
3. **所有内容层改动都主动告诉用户，改动报告由 `diff_report.py` 生成，不靠回忆。** 曾有一次标点规范化脚本的正则 bug 静默删掉约 85 处括号内容，全靠它揪出来。
4. **已入库的正文默认只读。** 只有用户明确要求的校勘流程才改正文，且每处改动都记进校注；用户自己的标注一律保留。

## 结构

```
SKILL.md                      主流程
scripts/
  mineru_api.py               MinerU 精准 API 配置、上传、轮询、下载、拆分与用量报告
  paddle_api.py               PaddleOCR 官方 API：令牌、分块提交、逐页存盘、检查新模型
  pdf_preflight.py            提交前本地预检文字层
  probe.py                    探查、圈码↔脚注对齐审计、七类自动检查
  build.py                    从 layout.json 重建骨架
  split_book.py               正文合成一份、卷首书末另存、出索引
  verify.py                   结构自检
  diff_report.py              交付前改动比对
  copy_images.py              只拷成品引用到的图片
  crop_pdf.py                 裁图核对 / 剥离文字层供重跑 OCR
  line_map.py                 逐行映射（逐块、逐页退路），可出文字层清单
  pdf_textlayer.py            按清单重建 PDF 隐形文字层（竖排整列一行）
  vl_common.py                VL 块标签与墨迹切行
  vl_build.py                 PaddleOCR-VL 作底稿时整理 Markdown（推断页码、按分隔线认注、拦编造内容）
  vl_compare.py               正文与 PaddleOCR-VL 逐页比对，出疑字
  doubt_sheets.py             疑字截整行原图、拼图、给用户的分组拼图
  merge_judgement.py          合并各批 AI 判定、核对字段
  apply_doubts.py             把判定结果按偏移写回正文
  structure_audit.py          结构核查：注区核对材料、目录对篇题、按页任务、按页改动执行（定位串须唯一）、注号重编
  note_tasks.py               读丢的注号（<!--?fnref n-->）按页出任务、放回
references/
  mineru-api.md               MinerU 精准 API 限制、端点、配置与报告语义
  mineru-outputs.md           MinerU 产物文件各自有什么、缺什么
  templates.md                frontmatter / AI 校注 / 改动报告 模板
  vl.md                       PaddleOCR-VL：第二位读者、竖排书底稿、实测
  structure-audit.md          底稿的结构核查：顺序与理由、结果格式、实测的坑
  structure-prompts.md        结构核查各步的子代理提示词模板（派子代理时才读）
  ai-judging.md、judging-prompt.md   AI 看原图判定疑字
tests/                        pytest，不调用外部服务
```

## 状态

以下为作者逐本核对过成品的范围：在一篇期刊论文和一本 200 页专著上做过端到端验证（含无 skill 对照组）。竖排标点古籍（扫描件、强制 OCR）《黄道周集》497 页整本走通：PaddleOCR-VL 底稿 → AI 看原图判定疑字 → 结构核查（注、篇题、清单页、残留）→ 竖排文字层。**尚未覆盖**：表格密集型 PDF、双栏排版、英文文献、含公式的理工科文献。

逐行映射与 PaddleOCR 官方 API 是 2026-10 新加的，上面「实测」的数字是脚本核对的结果，作者还没逐本核对成品。

## 许可证

[MIT](LICENSE)。
