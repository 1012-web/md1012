# 古籍酷与看典古籍 OCR 接口

两家在工作流里的角色见 [SKILL.md](../SKILL.md)「识别来源各管什么」。本页只写调用、凭据、作为第二读者的参数和续跑规则。已有缓存直接用；只在真要调用时查状态和额度。

## 古籍酷（GJ.cool）Chat

- 官方公开说明：[Chat](https://www.gj.cool/chat)。`ocr` 与网页版、传统 API 的 OCR 能力相同，Chat 是 OpenAI 兼容封装，不是生成式聊天模型。
- 账户提供 Base URL 与 API Key；用 `gjcool_api.py configure` 隐藏输入保存。环境变量为 `GJCOOL_BASE_URL`、`GJCOOL_API_KEY`。
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

## 看典作为第二读者

比对流程见 [doubt-check.md](doubt-check.md)。看典特有的部分：

- `--kandian "<目录>|21"`：该目录的 `p0001.json` 对应原 PDF 物理第 21 页，`p0003.json` 对应第 23 页。不连续抽页写 `<目录>|107,142,145`，列表须与页文件数一致。可重复给分卷；同一物理页不可重复。
- 只比较 `message=success` 的页；有 `_run.json` 时还要求页状态为成功。
- 按 API 返回顺序拼接文字行，排除明确标为 `layout_classes_id=1` 的书眉/版心；可能误标的短纲文、标题仍要从原始结果做结构核查。不带行结构时兼容默认文本数组。
- 看典坐标和候选字留在原始 JSON 供核查，不替换底稿坐标，也暂未并入疑字条目。
- 标点单独输出页级线索；条目号倒序保留为分歧，不自动修正。
- 看典的大段差异只交结构核查，不直接标为 VL 编造。

2026-10-17 单页核图参考共 423 个汉字，忽略列明异体后看典全部匹配；连标点和数字的字符得分为 91.46%。此为单页结果，不能当成全书准确率。

## 结果与续跑

每页的原始响应、正文分别为 `pNNNN.json`、`pNNNN.txt`；失败响应为 `pNNNN.failure.json`。`_run.json` 每完成一页就更新，记录成功状态、耗时、请求次数、所用参数、PDF SHA-256、失败及未完成页。

- 仅跳过已记录成功、且存盘 JSON 仍能提取正文的页；错误 JSON 不阻止续跑。
- 同一输出目录拒绝混用不同 PDF、分辨率或 OCR 参数。切换模型、版面参数时建新目录。旧报告没有 PDF 校验值且有成功页时，也建新目录。
- 只对连接失败、408、429 和暂时性服务器错误最多重试两次；401/403 和 API 业务拒绝立即停止。失败或未完成返回退出码 2，参数/配置错误返回 1，全成功返回 0。

2026-10-17 的《明通鉴》第 21–30 页实测记录位于项目 `api-tests/2026-10-17/`；该目录属于试跑材料，不随 skill 分发。
