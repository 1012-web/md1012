# md1012

一个 Claude Code skill：把历史研究用的 PDF 整理成忠于原件、可核对出处的 Markdown，适合做 Obsidian 笔记，也适合交给 AI 检索。

- **忠于原件**：改字必须有 PDF 原图为据，校注与正文分开，改动报告由脚本逐字比对生成。
- **结构完整**：多级标题、页码，现代出版物另有脚注、图表和原有标点。
- **对得回原书**：正文能定位到 PDF 的页（现代出版物通常能到行或列）。

## 两条工作流

| 工作流 | 材料 | 重点 |
|---|---|---|
| A：论文和现代出版物 | 论文、专著、教材、现代重新排字的点校古籍（横排竖排都算） | 正文、脚注关系、多级标题、页码、图表、原有标点 |
| B：非现代排印古籍 | 刻本、旧刊本、抄本等原页的扫描或影印 | 页码/叶码、多级标题、识字核对；保留夹注文字 |

两条都由 AI 看目录原图重建标题层级，并用第二家识别结果对照原图核字。

## 识别服务与隐私

识别走云端 API，本机不用装模型：[MinerU](https://github.com/opendatalab/MinerU) 精准解析、[PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)-VL，古籍另可用看典、古籍酷。**PDF 会上传到这些服务的服务器**；未刊稿、有版权或保密顾虑的材料，请改用本地软件解析后再交给本 skill 整理。密钥用各脚本的 `configure` 在本机隐藏输入保存，不写进仓库。

## 安装与使用

把本仓库放进 Claude Code 的 skills 目录（或用 skill 管理工具链接过去）。在 Claude Code 里给出 PDF 或已有的识别结果并说明需求，skill 会自动触发。

脚本需要最新版 Python 3，以及 `pymupdf`、`pypdf`、`Pillow`、`numpy`、`PyYAML`。

## 文档

- [SKILL.md](SKILL.md)：入口。范围与分流、主流程、保真规则、页码口径、书目录约定。
- 分支：[A 现代出版物](references/modern-publications.md)、[B 非现代排印古籍](references/historical-editions.md)。
- 共用步骤：[目录与多级标题](references/toc-headings.md)、[疑字核对](references/doubt-check.md)、[结构核查](references/structure-audit.md)。
- 接口：[MinerU](references/mineru-api.md)、[PaddleOCR-VL](references/vl.md)、[看典与古籍酷](references/guji-apis.md)。

## 测试与边界

`python -X utf8 -m pytest` 运行脚本测试，不调用外部服务。

已有论文、现代专著和现代竖排古籍的试跑记录；印刷影印页、手写影印页只有小样本，不能外推成全书准确率或适用于所有古籍。

## 许可

MIT，见 [LICENSE](LICENSE)。
