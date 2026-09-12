# 可逐字对应 PDF 的 Markdown：显式选择流程

只在用户明确要求逐字对应或字句精确定位时读取。普通 Markdown 转换、重新 OCR 不自动进入此分支。每份新材料重新判断模式。

## 交付与前提

默认只生成文件。不要安装/更新插件、启动 Obsidian、创建任务或发布代码。用户另行要求界面验收时才操作应用；文件检查通过不能写成已通过真实点击验收。

先完成普通转换的标题层级、跨页拼接、分章、脚注重编号、页码和 diff；已有成品直接读取，不为创建坐标改写正文。原 PDF 不修改，已入库教材正文只读。Markdown 保留页码锚点和脚注；几何坐标、字符区间、哈希、重建参数外置。

必须取得原 PDF、最终 Markdown、每个解析分卷的 layout/middle JSON 和受支持的 model JSON 视觉行框。分卷顺序以 API 报告里的原 PDF 物理起页为准，不能按 part 目录名称猜测。物理页为 1-based；印刷页用于引证，卷首可有独立偏移和逐页例外。没有可靠页序或视觉行框时，报告缺少什么并补齐后继续，不平均分配整段坐标。

有些新版 model JSON 的结构与现有桌面输出不同。先运行 check；不支持时报告该结构，不冒充能直接处理。目录内自定义章节文件名通过 markdownFiles 显式列出，避免误摄入索引、校注和用户笔记。

## 可执行入口

工具随 skill 提供，不依赖当前考研仓库。以下路径相对于 skill 目录；运行 Python 一律加 `-X utf8`。

```text
python -X utf8 scripts/pdf_sync.py check --config <配置.json>
<隔离环境Python> -X utf8 scripts/pdf_sync.py generate --config <配置.json>
<隔离环境Python> -X utf8 scripts/pdf_sync.py rerun --config <配置.json> --pages 201-201
```

check 只检查本地输入和依赖元数据，不导入 OCR 引擎、不下载权重、不上传文件。仅逐字模式需要时创建隔离 Python 3.11 环境，按 `scripts/sync-requirements.txt` 安装依赖。首次 generate 可能下载公开模型权重；不会把教材发送给 PaddleOCR 服务。模型文件和虚拟环境不随 skill 分发。

推荐使用 uv 创建环境，然后安装该独立依赖清单；可复用经版本检查的现有 Python 3.11 OCR 环境。依赖检查只是可用性检查，真实模型运行仍需小样验证。正式处理前选正文、脚注、复杂排版的代表页核对原图，记录本地每页耗时；不要承诺固定 OCR 准确率。

配置示例（路径可含中文，使用 UTF-8 JSON）：

```json
{
  "mode": "character",
  "vault": ".",
  "bookId": "example-book",
  "title": "示例书",
  "pdf": "materials/book.pdf",
  "markdownRoot": "materials/book",
  "markdownFiles": ["01-正文.md", "02-附录.md"],
  "layoutParts": [{"path": "raw/layout.json", "physicalStart": 1}],
  "modelParts": [{"path": "raw/book_model.json", "physicalStart": 1}],
  "printedPageOffset": 40,
  "fileOffsets": ["02-附录.md|42"],
  "pageOverrides": ["01-正文.md|-36|4"],
  "pageRangeOffsets": ["01-正文.md|423-893|26"],
  "output": "90-工作流/MinerU联动/example-book",
  "ocrBackend": "onnx",
  "onnxModel": "PP-OCRv6_medium_rec",
  "onnxDevice": "auto",
  "writingMode": "horizontal-tb",
  "recognitionModel": "PP-OCRv5_mobile_rec",
  "recBatchSize": 16,
  "renderScale": 2.0
}
```

`vault`、原始 JSON 路径相对于配置文件目录；pdf、markdownRoot、output 相对于 vault；markdownFiles 相对于 markdownRoot。示例页码只解释参数，不可用于其他书。页码偏移的优先级：`pageOverrides`（单页例外）> `pageRangeOffsets`（同一文件内某段印刷页用另一偏移，如上下册合订本）> `fileOffsets`（整个文件）> `printedPageOffset`（全书默认）。

三者的写法在 check 阶段就校验，不留到 OCR 跑完才发现：`pageRangeOffsets` 必须写全 `文件|起页-止页|偏移` 三段（单页也要写成 `61-61`，缺段或写成 `文件|61|3` 一律报 `page range offset must look like 'file|start-end|offset'`），区间必须升序；`fileOffsets` 是 `文件|偏移`，`pageOverrides` 是 `文件|印刷页|物理页`。三者引用的文件名都必须出现在 `markdownFiles`（未列时即 `[0-9][0-9]-*.md` 实际匹配到的文件），名字写错会直接报错——以前写错只是静默失配、整段按全书默认偏移跑偏。同一文件内的 `pageRangeOffsets` 区间不得互相重叠。

`cache` 可省略：省略时 OCR 缓存写到本机用户目录 `~/.mineru-pdf-sync/cache`（环境变量 `MINERU_SYNC_HOME` 可改基目录；推荐虚拟环境也放 `~/.mineru-pdf-sync/venv`），不进入仓库。配置文件本身应放在仓库内（推荐 `90-工作流/MinerU联动/_config/<bookId>.json`），生成时 catalog.json 只记录该配置的仓库相对路径 `rebuild.config`，不写任何本机绝对路径；配置放在仓库外时会往 stderr 打一条 WARNING，说明这本书不会有 `rebuild.config`、插件无法只凭仓库重跑页面（这不是错误，生成照常完成）。catalog.json 里同一 `id` 的旧条目按字段合并：本次运行没提供的字段（如上一轮记下的 `rebuild`）保留，提供的覆盖；写入用临时文件加原子替换，中途失败不会留下半截 catalog。MinerU 的 layout/model JSON 也应复制进仓库（推荐 `90-工作流/MinerU联动/_source/<bookId>/part-NN/`），仓库搬家后仍能重建。当前考研仓库默认目录为 `90-工作流/MinerU联动/`，其他仓库可配置 output；消费插件必须使用相同目录。未列 markdownFiles 时只读取编号章节 `[0-9][0-9]-*.md`。

引用仓库内已有 PDF；外部 PDF 必要时原样复制到材料目录，用完整文件哈希检查一致性，并对副本添加精确 Git 忽略规则。已有同名不同内容文件不得覆盖。不要自动删除 PDF、缓存或旧输出。

## 字位与修改兼容性

识别模型给出每个字在 CTC 时间轴上被解码出来的列号（PaddleOCR 走 `return_word_box`，ONNX 走 argmax 序列的下标，两者同义），字框沿识别列中心和相邻边界计算；这是 OCR 支持的字符位置，不是像素级字形轮廓。先渲染 PDF 原图并裁取视觉行，因此不依赖其文字层；是否重新提交 MinerU 单独判断。

MinerU 的 model JSON 只对正文给 `ocr_text` 视觉行框，页脚脚注（`page_footnote`）以及 `index`、`ref_text`、`image_caption`、`image_footnote`、`aside_text` 等块往往只有一个整块矩形，块内没有任何行框，按行裁图就一个字都读不到。入口因此在识别前补一步：对「有文字却矩形内没有任何行框」的文本块，用 PaddleOCR 文本检测模型（默认 `PP-OCRv5_mobile_det`，`--det-model` 可换；权重取本机 `~/.paddlex/official_models/` 下已有的那份，不联网另取）只对该块的裁图跑检测，把行多边形换算回页面归一化坐标，作为补充行框按 y 排序并入该页行集，与已有行重叠的检测结果丢弃。裁图四周先留出约半个块高的白边（上限 32 像素）再送检测——只有一行的短脚注紧贴着裁会得到一条二十几像素高、没有上下文的窄图，检测器什么都返回不了；白边带进来的邻行按「行框中心是否落在原块矩形内」筛掉，留下的行框再夹回原块内。同一横排上的多个检测框合并成一条（检测常把脚注的「①」和后面的句子分成两框，两框一行既会错放字位、又会低估读到了多少）。检测结果**高度**覆盖不到块高一半、或**面积**覆盖不到块面积一半时判为没读到（只咬到「①」这类标记时高度满格而面积极小，所以两个条件都要过），改走兜底：块高不超过该页正文中位行高的 1.6 倍（页面上没有行框时用固定阈值 0.025 归一化高度）就整块当一条行框；块更高则用水平投影按行间空白切行，每条再按该行的墨迹收紧左右边界，切不出来就整块当一条。投影的并行间隙：有正文行时取 0.35 个正文行高，没有正文行时先按严格相邻切一遍、用切出来的中位行高当尺子（按块高去猜会把整张目录页并成一行）。最终保证：有文字的块不会一条行框都没有。检测器懒加载、全书复用；整页都有行框的页面既不渲染也不加载检测器。抬头、页码、页脚、表格和公式块不补：它们的文字不在 Markdown 里，补进来只会给按序对齐塞噪声。检测图与识别图共用同一次页面渲染。`--no-line-detection` 可关掉这一步（关掉后脚注仍是 0 字位）。补行框在 PaddleOCR 与 ONNX 两个后端都生效（检测模型两边都用 PaddleOCR 那一份）；Tesseract/Kraken 分支不补，以免给「不装 Paddle 也能用」的备选引入 PaddleOCR 依赖。

### 两种识别后端

入口启用 PaddleOCR 与 ONNX Runtime 两种识别后端，二者的字框口径已验证等同。底层快照虽保留 Tesseract/Kraken 代码，它们的按行分配字框没有完成精确能力验证，禁止通过此入口交付为精确结果。竖排与古籍必须先核对识别顺序和模型表现，不能用横排教材的验收代替。

`ocrBackend` 选 `paddle` 或 `onnx`，其余配置项都一样。两者共用同一段裁图代码、同一套「CTC 列区间→字框」换算，**喂同一份权重时识别结果逐字相同**：在 world-ancient-lower 物理页 41–60 上逐行比对（batch=1，PP-OCRv5 mobile），683 行**字符串全等**，每个字框四个坐标的**最大绝对差为 0**，CPU 与 DirectML 两条路都是。

两个后端的**默认模型现在不是同一份**：ONNX 默认 `PP-OCRv6_medium_rec`（DirectML 上跑得动的最好的那档），Paddle 后端仍默认 `PP-OCRv5_mobile_rec`。这么分是因为 Paddle 路径是「显卡不灵时还能跑」的回退，本机只留着 v5 mobile 那一份 Paddle 格式权重，换默认值会让回退路径去联网下载。所以换后端跑同一本书，文字会有细微出入（不是坐标口径变了，是模型变了）；要逐字对照两个后端，得显式把 `onnxModel` 和 `recognitionModel` 设成同一个模型。

模型目录都从 PaddleX 官方源取：`official_models.get_model_path("<模型名>", model_formats=["onnx"])` 落到 `~/.paddlex/official_models/<模型名>_onnx/`，里面是 `inference.onnx` 加 `inference.yml`。**字典和输入规格一律从该目录的 `inference.yml` 读，不按模型名写死**：字典取 `PostProcess.character_dict`（v5 是 18383 字、v6 是 18708 字，顺序也不同），输入规格取 `PreProcess.transform_ops` 里 `RecResizeImg.image_shape`（v5 与 v6 眼下都是 3×48×320，写死就等于赌下一代不改高度）。会话建好后还拿 ONNX 图里声明的静态维（通道与高）和 yml 对一次，对不上直接报错——两份文件是分开下载的，配错对不会有任何症状，只会安静地按错高度缩放、出一整本乱码。

ONNX 专有配置项：

| 配置项 | 命令行 | 取值 | 说明 |
|---|---|---|---|
| `onnxModel` 模型 | `--onnx-model` | `PP-OCRv6_medium_rec`（默认）、`PP-OCRv5_mobile_rec`、`PP-OCRv5_server_rec`，或含 `inference.onnx` 的目录路径 | 名字会映射到 `<模型名>_onnx` 目录 |
| `onnxDevice` 设备 | `--onnx-device` | `auto`（默认）、`dml`、`cpu` | `auto` 按 DXGI 枚举挑专用显存最大且不小于 1 GiB 的适配器，挑不到就退回 CPU；`dml` 强制走 DirectML，枚举不到独显也不退回 |
| `onnxDeviceId` 适配器号 | `--onnx-device-id` | 非负整数 | 显式指定 DXGI 适配器序号，覆盖自动挑选 |
| `recBatchSize` 批量 | `--rec-batch-size` | 1–256，默认 16 | 两个后端共用 |

**设备选择要留神**：DirectML 的 `device_id` 就是 DXGI 适配器序号，而 0 号往往是核显。本机实测 0 号是 Intel 核显、1 号才是 RX 9070 GRE；照 DirectML 的默认取 0 号，速度比独显慢一个量级。所以 `auto` 与 `dml` 都按专用显存挑卡，只有显式给了 `--onnx-device-id` 才照办。实际用到的 provider 与显卡名写在 mapper 收尾行：`backend=onnx device=dml:AMD Radeon RX 9070 GRE pages=… mapped=…`。

实测（world-ancient-lower 物理页 41–60，MinerU 的 658 条 `ocr_text` 行，renderScale 2.0，只计识别、不含行框检测；每个组合跑两遍取快者）。准确率是**字符准确率** `1 - 编辑距离 / 答案字数`，答案取 `03-正文.md` 按页锚切出的该页正文，两侧都只留 CJK 与字母数字：

| 模型 / 设备 | batch | 20 页耗时 | 每页 | 字符准确率 | 最差页 |
|---|---|---|---|---|---|
| PP-OCRv5_mobile / DirectML（RX 9070 GRE） | 16 | 11.24 s | 0.562 s | 99.64% | 98.74% |
| **PP-OCRv6_medium / DirectML（RX 9070 GRE）** | 16 | 13.83 s | 0.692 s | **99.85%** | 99.43% |
| PP-OCRv5_mobile / DirectML | 1 | 19.70 s | 0.985 s | 99.64% | 98.74% |
| PP-OCRv6_medium / DirectML | 1 | 23.74 s | 1.187 s | 99.86% | 99.54% |
| PP-OCRv6_medium / CPU | 16 | 150.43 s | 7.521 s | 99.85% | 99.43% |

更早一轮（同样 20 页，batch 16）的 v5 数据留作对比：paddle / mobile / CPU（oneDNN，9 线程）81.8 s、onnx / mobile / CPU 88.5 s、onnx / server / DirectML 10.6 s。

要点：

- **v6 medium 比 v5 mobile 少犯约六成错**（错误率 0.36% → 0.15%），20 页里 15 页更准、3 页持平、1 页退 0.11 个百分点。代价是慢 23%（每页 0.562 s → 0.692 s）。
- **提速全部来自 DirectML**：v6 medium 在 CPU 上要 7.5 s 一页，DirectML 上 0.69 s，快 10.9 倍。没有独显就别用 medium，退回 v5 mobile。
- DirectML 与 CPU 的 v6 结果**逐行 100% 相同**（658/658），不存在精度打折。
- DirectML 接下 v6 图里 188 个节点中的 186 个，落在 CPU 上的 2 个（一个 Slice、一个 Concat）是 ORT 主动判定「CPU 更快」的 force fallback，不是算子不支持（v5 mobile 同理，301 个节点里 8 个落 CPU）。查法是把 `SessionOptions.log_severity_level` 开到 0，读 ORT 的节点放置日志——`get_providers()` 只说注册了哪些 provider，不说图实际落在谁身上。
- 批量会轻微改变结果：v6 在 batch 16 与 batch 1 之间有 14/658 行不同（补零宽度不同所致），但两档准确率只差 0.01 个百分点。生产用 batch 16。
- 脚注小字另抽两页看（world-ancient-lower 物理页 81、13，行框取流水线自己检测出来的那批）：p81 v5 98.99% → v6 99.66%（v5 漏掉「教的学问」四字并串出一个杂字），p13 两者都 100%。注意 p81 的「答案」本身是 MinerU 抽出来的文本，把「彦悰」错成「彦惊」，v6 读对了反而被算成一个错——v6 的实际差距比表上更大。
- v6 偶尔把全角括号读成半角（p13 的「（在今内蒙古…」）。标点不计入上面的准确率，但会进对齐；实测 8 本覆盖率没有因此变差。

本机 `~/.paddlex/official_models/` 里**在用**的是三份：`PP-OCRv6_medium_rec_onnx`（ONNX 识别）、`PP-OCRv5_mobile_det`（补行框检测）、`PP-OCRv5_mobile_rec`（Paddle 后端回退）。`PP-OCRv5_mobile_rec_onnx`、`PP-OCRv5_server_rec_onnx`、`PP-OCRv5_server_rec` 已删（省 179 MB）；要回头跑 v5 的 ONNX 得先重新拉一次，命令见上，报错信息里也写了。

依赖：Windows 装 `onnxruntime-directml`，其他平台装 `onnxruntime`，见 `scripts/sync-requirements.txt`；`pdf_sync.py check` 在 `ocrBackend=onnx` 时把 onnxruntime 一并列进依赖探测。**PaddleOCR 仍然必装**——补行框用的文本检测模型是 PaddleOCR 的，ONNX 后端只替换识别这一步。

OCR 缓存按后端隔离，互不串用：缓存目录的身份串里带上后端名、ONNX 模型文件的 sha256、实际 provider（cpu/dml）、适配器号与批量大小；mapper 侧的缓存子目录也按 `onnx-<模型目录名>-<provider>-b<批量>` 分开。注意 `sync_mapper.py` 本身的哈希也在身份串里，**改动脚本会让全部既有 OCR 缓存失效**，下一轮生成要重跑识别。

### 目录页的引导点

对齐前两侧文本都做 NFKC 归一化。目录行的引导点在这里会出事：Markdown 写两个 `……`，NFKC 把每个省略号展开成三个句点，得到六个点；纸面上却是几十个点，OCR 还会把其中几个读成 `·`。点数永远对不上，多出来的点被当成删除，连带把点后面的页码挤出对齐窗口。

因此归一化后再加一步：**长度不小于 2 的点号连串整段折叠成一个记号**（认 `.` `·` `•` `‧` `・` `⋯`，两侧一视同仁），折叠出来的那一个记号把整串点的来源字符都挂着，对上之后每个 `…` 都拿到坐标，拿到的是整串引导点的外接框。长度为 1 的单个 `·` 一律不碰——那是正文里真正的间隔号（`工人运动的发展·宪章运动`），折叠它会毁掉正文对齐。

实测这一步对目录页是小幅改善（world-modern-lower 的 `02-目录.md` 2969 → 2982 字），对正文无影响。

**目录页覆盖率低的主因不在引导点，而在分页**：MinerU 产出的 `02-目录.md` 往往缺 `<!--p.N-->` 页锚，整份目录的字因此全落在同一张物理页上，第二页之后的条目拿页面一比对全部落空。world-early-modern-lower 的目录横跨物理页 4/5/6，缺页锚时只有 32.11%（341/1062）；把三个块按真实页分别对齐后是 96.70%（1027/1062）。修法是给这类 Markdown 补页锚——这动的是源文件的页面语义，须由仓库主人决定，mapper 不擅自猜。

Markdown 的标题符号、加粗、普通链接、`[[内链]]`/别名、`<mark>/<u>/<s>` 标签和 `%%@id 备注%%` 不计入 PDF 正文字位。正文源码不修改，UTF-16 偏移仍对应包含这些标记的原始字符串；跨行或跨页的字位分开记录。

已有插件会保守重定位格式修改、改字、插删及段落拆合。格式与标注不改变正文；错字修订只能保留可靠原位置或激活已验证 OCR 候选。用户新增文字没有原 PDF 对应字位，不得猜测；大幅改写需重新对齐/人工复核，不能承诺任意修改后自动恢复。标签、data-id、备注必须完整保留。

## 输出、验收与补修

入口输出 schema v1 的 catalog、index、分章 gzip sidecar、coverage-report.md 和 delivery-report.json。兼容现有插件所需的正文匹配文本仍存在 sidecar 中；不在 Markdown 中添加坐标/定位 ID，不承诺所有书的 sidecar 固定仅几 MB。

每次运行保留旧输出：目标已存在时生成带时间后缀的新目录，catalog 指向新版本。重跑只强制刷新所选物理页的缓存，但仍加载其余页并重建全书索引，不把页子集当成全书覆盖。缓存按 PDF 内容、视觉行源、识别模型/版本、渲染比例和排版方向隔离，避免不同书同页号串用字位。缓存文件名在页号之后还带该页**全部行框**的短哈希（`p0024-<hash8>.json`），行集一变就不会再命中旧结果——补出的脚注行框正因此不会被旧缓存挡住。补行框之前写下的旧缓存名为 `p0024.json`，只覆盖正文行；遇到这种文件时入口不整页重跑，而是只对补充行裁图识别，把新字符追加到旧缓存字符之后，写出带哈希的新文件，旧文件保留不删（脚注在页底，Markdown 里脚注文字也排在该页正文之后，追加顺序正确）。追加仅在补充行整体排在正文行之后时成立，否则退回整页识别。检测结果另有一层缓存 `lines/p0024-<blockhash>.json`，按待补块矩形、检测模型和**补行框算法版本**（`LINE_DETECTION_ALGORITHM`，现为 `det-v2`）命名，命中时连页面都不用渲染；算法一改版本号就要跟着改，旧版本留下的结果（尤其是短脚注那批空结果）才不会被重放。识别缓存 `pNNNN.json` 的追加路径不受影响，照旧复用。

验收必须核查正文、标题、脚注的块级对应和跨页覆盖；正文/脚注块有缺失则标记未通过。机器检查有块不等于语义正确，需对照代表页原图核查。报告逐字覆盖率、候选坐标、缺失源码区间和相关物理页，保留可靠字位；没有字位的部分不能声称精确。图片/表格首版只作块级。

`delivery-report.json` 保存输入哈希和运行耗时；块级未通过或哈希不匹配时入口**退出码为 3**，`SYNC_REPORT <报告路径>` 那行照常输出，文件供复核而非宣称验收完成。退出码 2 留给 argparse 的命令行用法错误（参数拼错、缺 `--config`、`rerun` 没给 `--pages`），调用方据此区分「跑完但没过验收」和「命令行写错了」。哈希按插件现有换行归一化规则检查，并额外比较原始文件字节哈希，证明生成过程没有修改输入。

逐字有缺失可以先交付，不自动无限重试。提示用户是否选择追加修复，列出涉及页数、抽样测得的每页耗时及估计范围。首次模型加载与缓存命中会影响耗时，不能凭空填分钟数。资料不足时直说尚不能估时。

- 本地 PaddleOCR 补跑：更长本地耗时，MinerU API 额度消耗为 0。
- 重新提交 MinerU：另行说明上传页数、分卷数和本机估算额度；先取得本次选择，不把本地修复意愿当成重新上传授权。

最后交付明确区分“文件验收”与“Obsidian 实机验收”，列出实际通过项目、残余问题和产物路径。当前考研仓库追加维护日志，其他仓库遵循其规则。
