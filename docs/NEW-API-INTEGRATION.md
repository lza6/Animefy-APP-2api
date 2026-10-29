# Animefy → new-api 对接文档（自包含，可直接交给另一个会话执行）

> 本文档不需要读 APK、不需要读 `REVERSE-REPORT.md` 就能完成对接。
> 所有协议细节均为**实测验证**，字段名逐字为准。

---

## 0. 一句话

上游是 `https://sci.videomate.cc` 的 `/algo/v1/faceFusion/**`，**匿名可用**（自造 userid + `md5(signKey+秒级ts)`），
链路 = 上传图 → 建任务 → 轮询 → 拿 CDN 图。可对接成 **图生图/风格化** 能力，**不是文生图**（prompt 被上游忽略）。

---

## 1. 协议速查（实现渠道时照抄）

```python
BASE      = "https://sci.videomate.cc"
SIGN_KEY  = "mhmjYS"
PACKAGE   = "com.picastory.ins.editor"
VERSION   = "10186"      # versionCode
PLATFORM  = "1"
USER_ID   = 任意 20 位小写字母数字（客户端自造，服务端不校验）

def headers(json_body=True):
    ts = str(int(time.time()))                     # 必须秒级
    h = {
        "userid":    USER_ID,
        "userId":    USER_ID,                      # 上传用 userId，任务用 userid，两个都带最稳
        "timeStamp": ts,
        "cp":        PACKAGE,
        "version":   VERSION,
        "platform":  PLATFORM,
        "sign":      md5(SIGN_KEY + ts).hexdigest(),
    }
    if json_body: h["Content-Type"] = "application/json"
    return h
```

### 1.1 上传

```
POST {BASE}/algo/v1/aiGenerate-aigc/multipleUpload?ifHttps=true
multipart/form-data，字段名必须是 files，Content-Type: image/jpeg
→ {"c":200,"m":"成功","d":["https://algo.videomate.cc/original/261030/<uuid>.jpeg"]}
```

### 1.2 建任务

```
POST {BASE}/algo/v1/faceFusion/createAiStyleTask?ifWise=true
{
  "styleParams":{
    "style":"aiGenerate",
    "params":"{\"effectType\":\"<ID>\",\"width\":<w>,\"height\":<h>,\"oldPicture\":\"1\"}",
    "requestUrls":[{"index":0,"imgUrl":"<上传得到的URL>"}]
  },
  "level":0,
  "token":"",
  "width":1080
}
→ d: {"jobId":"…","jobStatus":"doing","responseUrls":null,"jobReason":null,"waitTime":1}
```

注意：`params` 是**字符串**（双层 JSON）；外层 `width` 是**屏幕宽度** 1080，不是图片宽。

### 1.3 轮询

```
POST {BASE}/algo/v1/faceFusion/queryAiStyleTask?ifWise=true
{"jobId":"<jobId>"}
→ d: {"jobStatus":"doing|success|fail","responseUrls":["https://algo-v1.videomate.cc/temp/…"],"jobReason":null}
```

建议轮询间隔 3s、总超时 ≥ 300s（实测 gen 引擎单次 26.7s，超分 11s，人像 14s，长尾可达 60s+）。

### 1.4 错误信封

`{"c":200,"m":null,"d":{…}}`；`c!=200` 视为业务失败：

| c | 含义 |
|---|---|
| 1001 | 参数错误（缺字段） |
| 1003 | 参数非法（枚举/格式） |
| 500 | 上游内部异常（含 NPE） |
| 3035 | 任务类型不匹配（例如用图像端点查视频任务） |

---

## 2. 模型映射（只接已验证的）

| new-api 模型名（建议） | effectType | 上游引擎 | 实测输出 | 耗时 |
|---|---|---|---|---|
| `animefy-enhance` | `2402` | imagesharpen（锐化/超分） | 恒 1024×1024 | 5–12 s |
| `animefy-gen` | `9999` | PL_60016_IMG_HA | 768×1376（跟随请求） | 20–30 s |
| `animefy-portrait` | `2501` | gx_img2img_speed_2511 | 792×1320（恒竖版） | 8–20 s |

> **不要**把 2101/2201/2301/2901 当可用模型上线：提交受理但 `jobStatus=fail, reason=-1`（缺参数，本轮未攻破）。
> **不要**承诺尺寸：`2402`/`2501` 不跟随请求尺寸。
> **不要**宣传"AI 绘画/文生图"：prompt 被上游忽略，输出是固定风格模板。

---

## 3. 方案 A（零改码，最快验证）：本地网关 + OpenAI 兼容渠道

`2api/demo_server.py` 已经暴露 OpenAI 形状接口：

- `GET /v1/models`
- `POST /v1/images/edits`（multipart：`file` + `model` + `size` + `response_format`）
- `POST /v1/images/generations`（JSON，必须带 `image`：b64 / dataURL / http URL）

步骤：

1. 起网关（生产机装好 `requirements.txt`）：
   ```powershell
   $env:ANIMEFY_MODE="real"; $env:ANIMEFY_PROXY="http://127.0.0.1:10808"
   $env:MAX_CONCURRENCY="4"; $env:REQUEST_TIMEOUT="300"
   python demo_server.py --port 8937
   ```
2. new-api 新建渠道：类型选「OpenAI」，Base URL 填 `http://127.0.0.1:8937`，密钥随便填（网关不校验）。
3. 模型映射填 `animefy-enhance→animefy-enhance` 等三条。
4. 价格：按 **次** 计价，别按 token；建议先 0.02–0.05 美元/次（`2402`），`9999` 更高。
5. 超时：渠道/系统设置里把单请求超时调到 **≥ 300s**。

**已知风险**：new-api 的 OpenAI 图片 relay 默认走 `/v1/images/generations` 且可能不带 `image`，会被网关以
`this gateway is image-to-image only` 400 拒掉。验证时务必用 `/v1/images/edits` 路径（客户端上传图）或在
请求里显式带 `image`。若 400 频发，直接走方案 B。

---

## 4. 方案 B（推荐长期）：新增原生渠道 `animefy`（type = 80）

当前仓库渠道号用到 `79`（CodeAI），**下一个空位是 80**。参考同族实现：
`relay/channel/aivm/`（type 72，图生视频）、`relay/channel/aivg/`、`relay/channel/fravix/`。

### 4.1 需要改动的点（按仓库现有约定，逐处）

1. `constant/channel.go`：新增 `ChannelTypeAnimefy = 80` 与名称映射 `80: "Animefy"`。
2. `constant/api_type.go`：新增对应 `APIType`。
3. `constant/twoapi_channel.go`：**必须同步登记**（文件顶部注释明确写了"新增渠道类型时只改这一处，避免漏计费"）。
4. `relay/relay_adaptor.go`：`case 80: return &animefy.Adaptor{}`。
5. 新建 `relay/channel/animefy/{adaptor.go, do_request.go}`：
   - `ConvertImageRequest`：把 OpenAI images 请求体转成本文 §1.2 的双层 `styleParams`
   - `DoRequest`：先 `POST /algo/v1/aiGenerate-aigc/multipleUpload`（multipart `files`），再建任务
   - 轮询：`/algo/v1/faceFusion/queryAiStyleTask`，3 s 间隔
   - `DoResponse`：把 `responseUrls[0]` 包成 OpenAI `{"created":…,"data":[{"url":…}]}`
6. 前端渠道类型下拉：`web/src/` 里按现有逆向渠道的登记方式补一项（参考 aivm/aivg 的 i18n key）。
7. 定价：`setting/billing_setting/builtin_billing.go` 按「次」计价的写法（不要用 per-token 表）。

### 4.2 渠道字段

| 字段 | 值 |
|---|---|
| Base URL | `https://sci.videomate.cc`（若走代理则填本机代理网关） |
| 密钥 | 不需要；如必填，填 `mhmjYS`（签名 key）或任意占位 |
| 模型 | `animefy-enhance` / `animefy-gen` / `animefy-portrait` |
| 额外设置 | 超时 300 s；并发建议 ≤ 4（上游并发表现未测上限） |

### 4.3 实现要点（踩坑清单）

1. 签名 **秒级**；毫秒必 401。
2. `params` 必须 `json.dumps(inner)` 成字符串再塞进外层对象。
3. multipart 字段名**只能**是 `files`；`file/img/image` 会被回 `param illegal: files is empty`。
4. 上传的 `Content-Type` 用 `image/jpeg`；非 JPEG 输入先在网关侧转码。
5. `ifWise=true` 建任务/查询都要带。
6. `jobId` 前缀直接暴露引擎：`aiGenerate…`/`PL_60016_IMG_HA`/`60019…`/`hairRepaint…`/`22-…`/`4-…`，排错时先看前缀。
7. 失败判定有两个层次：HTTP 200 + `c!=200`（业务错），以及 `jobStatus=fail`（`jobReason=-1` 多为缺参）。
8. 上传返回的 CDN URL 有时效性，建任务要**立刻**用，不要缓存复用。

---

## 5. 验证四步（照做即可判定接入成功）

```bash
# 1) 网关活着
curl -s http://127.0.0.1:8937/healthz

# 2) 直连网关真实出图（约 10-15s）
curl -s -X POST http://127.0.0.1:8937/v1/images/edits \
  -F "file=@input.jpg" -F "model=animefy-enhance" -F "size=1024x1024" -F "response_format=url"

# 3) 经 new-api 出图（用带图上传的客户端，确保走 /v1/images/edits）
#    期望：200，data[0].url 可下载，图片 1024x1024

# 4) 计费核对：完成一次后检查额度扣减与日志 other.admin_info
```

判定标准：第 2 步返回 `data[0].url` 且能下载出图 → 上游链路 OK（**已验证**）；
第 3 步 200 且扣费正确 → 渠道 OK。

---

## 6. 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| `404` + 空 body | host 错（打到 `face-api.*`） | 换 `sci.videomate.cc` |
| `404` + Spring 信封 | 路径少了 `/algo` 前缀，或用了 `face-api.ufotosoft.com` | 对齐 §1 的完整路径 |
| `param illegal: files is empty` | multipart 字段名不是 `files` | 改字段名 |
| `param error: param effectType illegal` | 内层 `params` 没带 `effectType` | 补 `effectType` |
| `param illegal: token` | 用了 `/aigc/image2image/createAiStyleTask` | 用 `/faceFusion/createAiStyleTask` |
| `jobStatus=fail, reason=-1` | effectType 家族缺必要参数 | 只用 2402/9999/2501 |
| `c=3035 IMAGE2VIDEO_OTHER` | 该 effectType 是视频任务 | 换查询端点或放弃该 id |
| `NumberFormatException: null` | 用了 `/aigc/image2image/createImageTask` 但缺数值参数 | 改用 `createAiStyleTask` |
| 出图尺寸与请求不符 | 上游按引擎固定尺寸 | 前端不要展示"尺寸"选项 |
| 生成内容与 prompt 无关 | 上游忽略 prompt | 定位为"风格模板"，定价/文案同步改 |

---

## 7. 交接清单

- [ ] `requirements.txt` 装好（fastapi/uvicorn/requests/pillow）
- [ ] 网关 real 模式跑通一次真实出图（§5 第 2 步）
- [ ] 若走方案 B：渠道号 80 已在四处登记（channel.go / api_type.go / twoapi_channel.go / relay_adaptor.go）
- [ ] 定价按「次」，不是 per-token
- [ ] 超时 ≥ 300 s，并发闸门 ≤ 4
- [ ] 上线模型只有 3 个已验证 effectType
- [ ] 文案不承诺"文生图"、不承诺"指定尺寸"
