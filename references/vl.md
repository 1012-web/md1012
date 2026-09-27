# PaddleOCR-VL：第三读者（竖排、古籍、MinerU 漏块时用）

在 SKILL.md「竖排与古籍」一节指到这里时读取。它不是默认流程：横排教材、有好文字层的 PDF 用不着。

## 它是什么、在流程里干什么

PaddleOCR-VL 是视觉语言模型（0.9B 参数）：看整页图、先做版面分析（PP-DocLayoutV3 分出标题、正文块、页下注），再像语言模型那样把每块的字「写」出来，懂阅读顺序和上下文。与另外两家的分工：

| | MinerU（底稿） | 本机 PP-OCR（逐字映射） | PaddleOCR-VL（第三读者） |
|---|---|---|---|
| 认字依据 | 版面 + 识别 | 只看单字字形 | 看字形也看上下文 |
| 逐字坐标 | 无 | **有**（文字层靠它） | 无，只有块坐标 |
| 长处 | 结构（标题、脚注、跨页） | 字位；只看字形，原书异体读得忠实 | 字全、标点注号全、能找回 MinerU 整块漏掉的块 |
| 短处 | 竖排会整块漏读、丢注号 | 竖排标点大半读不出；近形异体（黃/黄）摇摆 | **按上下文「规范化」**：原书「遥産悦録」写成「遙產悅錄」，也可能把原书误印「改对」；嵌套引号『』会写成「」 |

默认只有两种用法：**补块**（vl_supplement.py：MinerU 漏掉的块 → 补充行框，交给逐字映射去读字定位）与**疑字线索**（vl_compare.py：与最终正文逐页比对，分歧交 AI 看原图或站长判）。不许拿 VL 的读法直接改 MinerU 底稿上的字。

**用户拍板以 VL 作底稿时**（MinerU 在这本书上明显不行），用 `vl_build.py`：

- `pages`：推断每页印刷页码。VL 读页码噪声大（「O」「111」「！！！！」），竖排页码「一三」常只读出「三」、连续几页自洽地错；插页又让偏移中途变化。规则：可解析且与邻页偏移一致才采信；≤7 页的一小段偏移与前后两段都不同、而前后两段相同，判为误读丢掉；两可信页偏移相同则中间按它补；篇首不印页码的页顺延 2 页以内。《黄道周集》推出：物理 1–9 无页码（封面版权简目）、10–16 目录 1–7、17–98 前言 1–82、99 起正文。
- `build`：按分段写成几个文件。页下注以**分隔线**为准（页面图上又细又长、两侧空白的墨线，竖排是竖线）——只看注号开头会把「（一）泛濫詩賦」这类小标题当成注；线那一侧至少一块以注号开头才认（版权页的表格线也是细长线）。VL 读正文注号很不稳定（试水前言 65 条注只有 18 条读到了注号），读丢的留 `<!--?fnref n-->`，之后看原图放。
- 放大一倍重读（`vl_parse.py --dpi 288`）救不回注号：《黄道周集》前言 82 页，1 倍读出 125 个注号，2 倍反而 113 个。读丢的注号只能看原图定位置。
- 标题层级：VL 多把篇题标成正文。竖排的篇题比正文低三四格起排（上沿在页高 17%–32%）、短、无句读；「卷N」在右侧第一列且本页另有文体标签或篇题才算卷名（书目页里的「卷二十 説」不算），卷号按出现顺序校正；前言里「一、」「（一）」分二、三级；篇题后原书小字的年月、「凡三章」「節録」用 `<small>` 标出。
- 图、表按 VL 块坐标从原 PDF 裁出存 `images/` 原位嵌入；VL 把文字区当成图（`chart`）时留 `<!--?chart-->` 待补录；段落字号比本页正文小两成以上的（「洪思曰」夹注）用 `<small>` 标出。
- **VL 会凭空编造**：物理 103 页一处空白写出「1986年4月24日，中国政府拨款……」并重复十遍，110 页写出「A 1000-A 1000-…」。整理时三条硬特征拦下（简体专用字 ≥3、同一串数字字母重复 ≥4 次、同一句重复 ≥3 次），写成 `<!--?vl-suspect 原因：前30字…-->` 并列入报告，交人工核对原图。
- 页边重复出现的短块（竖排页边的篇名、页码）当页眉丢掉：VL 常把它们标成正文，接段时会夹进句子中间。
- 报告里给出映射配置要用的页码偏移（`fileOffset`、插页之后的 `pageRangeOffsets`）与页码冲突。
- **vl_build 之后必须做结构核查**（[structure-audit.md](structure-audit.md)）：VL 底稿整本做完、逐字映射也跑过之后，《黄道周集》仍有 61 条注漏收或混进正文、17 个篇题读丢、清单页漏读约三分之二。这些逐字映射查不出来，要逐页核对注区、拿目录对篇题、清单页整页重录、扫残留。

## 实测（《黄道周集》4 页试水：竖排繁体、中华书局 2017 排印本、扫描件）

以逐页对原图核过的试水稿为标准（准确率 = 1 − 编辑距离 / 标准字数）：

| 来源 | 只比汉字 | 汉字（异体折叠） | 汉字＋标点（不计注号） | 标点读出 |
|---|---|---|---|---|
| MinerU | 84.9% | 88.1% | 88.8% | 245/271 |
| 本机 PP-OCR | 90.9% | 93.0% | 85.5% | 123/271 |
| PaddleOCR-VL（显卡或 CPU，逐字相同） | 96.7% | 99.6% | 99.9% | 269/271 |

- MinerU 整块漏掉的第 1 页署名与两条页下注，VL 全部读回；MinerU 的「治生活產」（原书「治生治産」）VL 读对。
- 原书字形 8 组异体里，VL 只「爲」全对，「産悦録遥」全被改成通行字形；PP-OCR 这四组反而全对。
- 速度：9070 GRE（Vulkan）每页约 6.5 秒（首页含预热约 11 秒），纯 CPU 约 45 秒；497 页一本约 1 小时（显卡）。

## 安装（本机做过一次即可）

1. **llama.cpp**（跑模型的推理程序）：GitHub `ggml-org/llama.cpp` 发布页下 Windows 的 `llama-<版本>-bin-win-vulkan-x64.zip`（约 31 MB），解压到 `~/.mineru-pdf-sync/llama.cpp/<版本>/`。Vulkan 版 AMD、Intel、NVIDIA 显卡都能用，也能 `--device none` 纯 CPU 跑。注意：2026-04 的 b8984 在 Windows + A 卡上出过「加载成功但零输出」（issue #22551），b8986 起有人确认修好；本机实测 b11177 正常。
2. **模型**：Hugging Face `PaddlePaddle/PaddleOCR-VL-1.6-GGUF` 的 `PaddleOCR-VL-1.6-GGUF.gguf`（892 MB）与 `PaddleOCR-VL-1.6-GGUF-mmproj.gguf`（841 MB），放 `~/.mineru-pdf-sync/models/PaddleOCR-VL-1.6-GGUF/`。版面模型 PP-DocLayoutV3（约 126 MB）首次运行时 PaddleOCR 自动下载到 `~/.paddlex/official_models/`。
3. **Python 依赖**：装进 OCR 隔离环境（`~/.mineru-pdf-sync/venv`，uv 建的，没有 pip）：`uv pip install --python <venv的python> "paddlex[ocr]==3.7.2" "openai>=1.63"`（见 `scripts/sync-requirements.txt`；只新增、不动已有包）。
4. 所有下载都先向用户报文件名、来源、大小，得到同意再下。

**显卡**：PaddlePaddle 自身的显卡加速只支持 NVIDIA（Windows 版要求计算能力 7.5 以上，PaddleOCR-VL 官方建议 8.0 以上即 RTX 30/40/50 系）；A 卡走 llama.cpp 的 Vulkan 就绕开了这一点。备选路线：WSL2 + ROCm 7.2.1（AMD 官方 WSL 支持表列了 RX 9070 GRE）+ vLLM，最快但搭环境最麻烦，且有 9070 XT 在 WSL2 下间歇卡死的报告；PaddleOCR 官方的 AMD 镜像只在 MI300X 上验证过，同代的 R9700 上 Paddle 原生部分崩溃（PaddleOCR issue #18300）。**llama-server 的 0 号 Vulkan 设备常是核显**，vl_parse.py 自动挑专用显存最大的那块。

## 竖排古籍的整条流程

以《黄道周集》为例（`<书>` = 书文件夹，`<py>` = OCR 隔离环境的 Python）：

```text
1. MinerU 精准解析（强制 OCR）→ 重建骨架、整理正文、校注、diff（SKILL.md 普通流程）
2. <py> vl_parse.py --pdf <书>/书.pdf --pages 1-497 --out <书>/_数据/VL --llama-dir … --model-dir …
3. python vl_supplement.py --pdf <书>/书.pdf --vl-dir <书>/_数据/VL --pages 1-497 \
       --model-part <MinerU>/xxx_model.json|1 … --writing-mode vertical-rl --out <书>/_数据/VL补块/model.json
4. 逐字映射配置：writingMode vertical-rl；modelParts 追加 {"path": "…/VL补块/model.json", "physicalStart": 1}；
   variantEquivalents 填逐组核实过的异体（SKILL.md「异体字方针」选了保留原书字形时）
5. <py> pdf_sync.py generate --config <书>/_数据/config.json   → 块级验收、覆盖率、推定坐标
6. python vl_compare.py --book <书> --vl-dir <书>/_数据/VL     → 汉字分歧交 AI 看原图判定（references/ai-judging.md）
7. python textlayer_spec.py --book <书> --output <书>/_数据/文字层/spec.json.gz
8. python pdf_textlayer.py build --pdf <书>/书.pdf --spec … --output <书>/书.校订版.pdf   （竖排自动整列一行）
```

第 2 步可中断续跑（已存的页跳过）。第 5 步后正文若因判定改了字，要重跑第 5 步再导出清单（第 7 步发现映射过期会拒绝导出）。

**VL 作底稿时**，第 1 步换成 `vl_build.py pages` + `build`，并在第 5、6 步之间插入：

```text
5a. 疑字判定并执行（ai-judging.md；书不在库里用 doubt_sheets.py + apply_doubts.py）——按偏移改字，必须在结构改动之前
5b. 结构核查（structure-audit.md）：清单页与目录重录 → 目录对篇题 → 注区核对与补注 → 残留扫描 → renumber、notes-count
5c. 重跑第 5 步映射，unmappedBlocks 逐块看
```

**竖排文字层与插件版本**：笔记1012 插件自提交 41531e8（分支 feat/vertical-textlayer）起支持竖排：清单带排版方向，读映射的推定框，缺字沿列排。更早的插件版本给没坐标的字横向排，竖排上会复制成一字一行，那时改用第 7、8 步的 `textlayer_spec.py` + `pdf_textlayer.py build`（竖排自动整列一行）。竖排映射里读不出的标点由映射程序推定位置（`estimated`），不冒充读出的字位。
