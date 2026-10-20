# 古籍酷与看典古籍 OCR API

看典可作为古籍按页文字来源的第二读者，现代出版物使用 VL 文字时也可按需采用。两条材料流程由 SKILL.md 决定，不以竖排直接归为影印古籍。古籍酷 Chat 保留为用户指定的备用测试接口。已有缓存直接复用；仅实际需要调用时检查状态及额度。新材料先抽样核图，保留原始响应，不直接覆盖正文。

## 古籍酷（GJ.cool）Chat

- 官方公开说明：[Chat](https://www.gj.cool/chat)。`ocr` 与网页版、传统 API 的 OCR 能力相同，Chat 是 OpenAI 兼容封装，不是生成式聊天模型。
- 账户提供 Base URL 与 API Key；用 `scripts/gjcool_api.py configure` 隐藏输入保存。环境变量为 `GJCOOL_BASE_URL`、`GJCOOL_API_KEY`。
- `POST <base>/chat/completions`；仅在 404 时再试 `<base>/v1/chat/completions`。Base 已含 `/v1` 时不重复追加。完整端点也可作为 Base。
- 图片以 JPEG data URL 放进 `messages[].content[].image_url`，`model=ocr`、`stream=false`。
- Chat 响应保存为 `pNNNN.json`，正文取 `choices[0].message.content`，另存 `pNNNN.txt`。本接口不保证返回行或字符坐标。
- [传统 API 文档](https://www.gj.cool/doc/v1) 的 API ID + password、`Authorization: gjcool ...` 属于另一套鉴权，不能与 Chat 的 Bearer key 混用。

```powershell
python -X utf8 <skill>/scripts/gjcool_api.py submit <PDF> -o <输出目录> --pages 1-10 --model ocr
```

## 看典古籍（kandianguji.com）

以官方[接口文档](https://kandianguji.com/api_documentation)和[多语言示例](https://kandianguji.com/article/51)为准。

**凭据是 token + token 表格中显示的「账号」**。参数虽然叫 `email`，手机号注册的账号应传手机号；之后绑定的邮箱不一定可替代。网页能登录、Token 显示可用，仍可能因账号不匹配而返回 `token无效`。先用状态接口定位，不反复上传图片。

```powershell
python -X utf8 <skill>/scripts/kandian_api.py configure
python -X utf8 <skill>/scripts/kandian_api.py status -o <状态报告.json>
python -X utf8 <skill>/scripts/kandian_api.py submit <PDF> -o <输出目录> --pages 1-10 --version v2 --det-mode sp --return-position --return-choices --det-layout
```

- 环境变量为 `KANDIAN_OCR_TOKEN` 和 `KANDIAN_OCR_EMAIL`（后者存账号，也可以是手机号）。Windows 配置用 DPAPI 加密 token；不在报告中记录 token 或账号。
- `POST https://ocr.kandianguji.com/get_token_status`：请求 `token`、`email`；不上传图片。响应 `data.is_active`、`total_count`、`used_count`，额度以该 token 的实时返回为准。
- `POST https://ocr.kandianguji.com/ocr_api`：Form Data；图片 base64，`version=v2`，`det_mode=sp`（竖排）/ `hp`（横排）/ `auto`，`image_size=2000` 为最长边，`0` 不缩放。
- `--return-position` 返回行与字符坐标；`--return-choices` 要与它同时使用，返回候选字。坐标对应服务端响应中的 `width/height`，不能直接当成原 PDF pt。
- `--det-layout` 开启版面识别并请求版面信息；客户端固定 `only_plain_text=false`，保留页眉、页脚、版心以供核查。
- 只有 HTTP 200 且 `message=success`、并提取到非空文本才算本次 OCR 成功。默认 `data` 是字符串数组；坐标模式为包含 `texts` / `text_lines` 的对象。空白页若返回空文本会标为需人工核查，不自动视作成功。
- API 的 `text_angel` 等字段是官方保留的拼写，不要擅自重命名。完整原始结构留在 JSON 中。
- 实测 `words[].choices` 是包含多个候选字的字符串，与文档数组示例不同，应同时兼容两种形式。`text_lines[].position` 与 `layout[].position` 也不能直接假定共用坐标系：300 DPI、`image_size=2000` 的样本返回 `width=1826,height=2688`，文字行坐标对应原渲染图，版面坐标则表现为缩小后的尺度；映射 PDF 前必须核图和比例。
- 官方文档已将 PDF 直传接口标为停用；客户端逐页渲染后调用图片 OCR。

## 作为第二识别者接入疑字流程

```powershell
python -X utf8 <skill>/scripts/vl_compare.py --book <书> --line-map <书>/_数据/逐行映射/line-map.json --kandian "<书>/_数据/看典|1" --pages 1-10 --out <书>/_数据/疑字判定
```

`--kandian` 可重复给分卷；`目录|21` 表示该目录的 `p0001.json` 对应原 PDF 物理第 21 页，`p0003.json` 则对应第 23 页。不连续抽页可写 `目录|107,142,145`，列表须与页文件数一致。同一物理页不可重复。`--kandian`、`--vl-dir`、`--mineru` 三选一；多家交叉核查时用不同输出目录。

读取器只比较 `message=success` 的页；有 `_run.json` 时还要求页状态为成功。按 API 返回顺序拼接文字行，排除明确标为 `layout_classes_id=1` 的书眉/版心；可能误标的短纲文、标题仍要从原始结果做结构核查。不带行结构时兼容默认文本数组。

沿用底稿的行映射生成 `doubts.json`，可直接交给现有裁图、判定、执行工具。看典坐标和候选字仍留在原始 JSON 供核查，不直接替换底稿坐标；暂未把候选字自动合并到疑字对象。标点单独输出页级线索，条目号倒序保留为分歧，不自动修正。

`--pages` 指定本次原 PDF 页范围；字形表默认读取书配置，`--variant` 显式覆盖。`_reader_coverage.json` 只对本次范围记录实际比较、第二读者缺页及正文未映射页，不要求补跑范围外页面；覆盖不全退出码为2。看典的“大段差异”只交结构核查，不直接标为 VL 编造。

2026-10-17 单页核图参考共 423 个汉字，忽略列明异体后看典全部匹配；连标点和数字的字符得分为 91.46%。此为单页结果，不能当成全书准确率。

## 结果与续跑

每页的原始响应、正文分别为 `pNNNN.json`、`pNNNN.txt`；失败响应为 `pNNNN.failure.json`。`_run.json` 每完成一页就更新，记录成功状态、耗时、请求次数、所用参数、PDF SHA-256、失败及未完成页。

- 仅跳过已记录成功、且存盘 JSON 仍能提取正文的页；错误 JSON 不阻止续跑。
- 同一输出目录拒绝混用不同 PDF、分辨率或 OCR 参数。切换模型、版面参数时建新目录。旧报告没有 PDF 校验值且有成功页时，也建新目录。
- 只对连接失败、408、429 和暂时性服务器错误最多重试两次；401/403 和 API 业务拒绝立即停止。失败或未完成返回退出码 2，参数/配置错误返回 1，全成功返回 0。
- 对照其它 OCR 的字符差异只表示分歧；未逐字核原图，不能称作准确率。标点、标题、脚注和坐标也要分别检查。

2026-10-17 的《明通鉴》第 21–30 页实测记录位于项目 `api-tests/2026-10-17/`；该目录属于试跑材料，不随 skill 分发。
