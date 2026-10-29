# Animefy v2.37.10186 逆向深度报告

> 目标：`Animefy v2.37.10186 (Premium).apk` / `com.picastory.ins.editor`
> 方法：jadx 1.5.1 全量反编译（31145 类，69 个方法反编译失败） + dex 字符串面全量导出 + 线上黑盒验证
> 结论等级标注：**已验证**（真实请求/观察）/ **静态确认**（读代码确认）/ **合理推断** / **待验证**

---

## 1. 判型与工程结构

| 项 | 观察 | 结论 |
|---|---|---|
| 语言/形态 | 无 `libflutter.so`，无 RN bundle；11 个 dex（classes.dex…classes11.dex，合计 ~75 MB） | **原生 Java/Kotlin 混合工程**，非 Flutter/RN |
| 核心 SDK | `assets/PhotoEditorSDK/`、`libSlidePlayer.so`、`libtsutils.so`、`libbeautytune.so`、`libtt_ugen_layout.so` | 涂图 TuSDK 系图像/视频编辑栈 |
| AI 相关原生库 | `libLurker.so`(2.8M)、`libimagecut.so`(3.2M)、`libfaceDetect.so`、`librthairseg.so`、`libanimateblend.so`、`libglfaceaging.so` | 人脸/发型分割、像素级本地特效 |
| 加固 | `libmatrix-*`(腾讯 Matrix 监控)、`libpglarmor.so`、`libsigner.so`、`libwechatbacktrace.so`；**无 pairip/SignatureKiller 类重打包残留** | 有完整性/监控组件，但**没有把业务协议塞进 native**（AI 接口全在 dex） |
| 广告/归因 | TopOn(anythink)、Pangle、AppLovin、Unity、Fyber、InMobi、TradPlus、Adjust、Firebase | 与协议无关，全部为噪声，dex 字符串约 40 万条里 90% 是它们 |
| 权限/包信息 | versionCode `10186`、versionName `2.37.10186`、minSdk 21 / targetSdk 36 | 签名时要带 `version=10186` |

**判断依据**：`unzip` 后 `lib/` 只有 `arm64-v8a`，且没有 Flutter/Dart AOT 特征；`assets/aigc/*` 只是 Lottie 动画。

---

## 2. 主机拓扑（静态确认 + 已验证）

| 主机 | 角色 | 实测 |
|---|---|---|
| `https://sci.videomate.cc` | **主 API**（retrofit base）+ **AI 云算法 API base** | `/` 404 JSON（Spring Boot），业务接口 200 |
| `https://face-api.videomate.cc/` | 备用/远程配置下发的 host | `/` 200 html；`/algo/...` 被 `stgw` 404 |
| `https://face-api.ufotosoft.com` | CloudAlgo host（`LaunchHelper.c()` 里硬编码 release/beta 分支） | 404 Spring 信封，`/algo/...` 未命中 |
| `https://sc-res.videomate.cc` | 静态资源/CDN、隐私政策页 | 根 503，资源 200 |
| `https://res.ufotosoft.com` | 图片/视频 CDN | 200/403 |
| `https://algo.videomate.cc` | 上传后图片落地 CDN（`/original/…`） | 200 |
| `https://algo-v1.videomate.cc` | 生成结果 CDN（`/temp/…`） | 200 |

`ServerRequestManager` 的四条 base（`静态确认`）：

```java
f66298n = "https://sci.videomate.cc";          // 主服务 retrofit base
f66299o = "https://face-api.videomate.cc/";    // aiface 备用
f66300p = "https://sci.videomate.cc";
f66301q = "https://face-api.ufotosoft.com";    // CloudAlgo 私有 host（k.java:164）
```

> 关键坑：`face-api.*` 这两个域名**不是** AI 接口的实际入口。真正入口是 `com.ufotosoft.base.net.b.a()`
> → `mb.b.a.d(null)` 的默认值 `https://sci.videomate.cc`（`LaunchHelper` 用它 `setAIGCHost`）。
> 一开始按 `face-api.ufotosoft.com` 打全是 404，就是这个原因。

---

## 3. 密钥链与签名

### 3.1 调用栈（静态确认）

```
AIGCServer.requestAIGC(...)                     com/ufotosoft/ai/aigc/AIGCServer.java
  └─ sign = db.a.g(signKey + (System.currentTimeMillis()/1000))      ← MD5 十六进制
       └─ db.a.g(String)                        com/…/db/a.java:130   MessageDigest("MD5")
  └─ mService.<createXxx>(userid, ts, cp, version, platform, sign, ifWise, body)
       └─ g extends j                         com/ufotosoft/ai/base/g.java（~50 个 @POST 注解）
```

`signKey` 来源链（**这是全案最关键的一跳**）：

```
LaunchHelper.launch()  →  q9.c.c().f(new yb.d().a("aQOWS13Le3U="))   // k.java:272
                          ↑ DES 解密字面量
yb.d(): DES/CBC/PKCS5Padding
        key = Const.DES_KEY = "IM41OUCa"
        iv  = {0x12,0x34,0x56,0x78,0x90,0xAB,0xCD,0xEF}
        plaintext = base64_decode("aQOWS13Le3U=") → "mhmjYS" + 0x02 0x02(PKCS5)
```

### 3.2 结论（已验证）

```
signKey = "mhmjYS"
sign    = md5("mhmjYS" + str(int(time.time())))     # 小写 hex，秒级
```

**秒级而非毫秒**：`AiFaceServer$uploadFaceImage$1` 里是 `System.currentTimeMillis() / 1000`，
而 `ServerRequestManager.G()/K()` 里同样是 `/1000`。若误传毫秒，签名必然不过。

### 3.3 userid 不需要账号（已验证）

APK 里 userid 来自 SharedPreferences `sp_key_appset_user_id`，是**首次启动本地随机生成**的 20 位串
（`com.ufotosoft.base.d.b()`）。黑盒验证：随便造一个 `probe0000000000000001`，所有 AI 接口正常
受理并出图 → **服务端只校验签名，不校验 userid 是否存在**。

### 3.4 反混淆副作用（重要工程坑）

jadx 会把一部分字符串常量**还原成 `n`**（因为常量池里存的是 `com.json.*` 静态字段引用）。例如：

- `@o("algo/v1/{engineName}/n")` 实际是 `multipleUpload`
- `jSONObject.put("style","n")` 实际是 `aiGenerate`（在 `AIGCServer$requestAIGC$1` 里能读对）
- `action.getStyle(),"n"` 实际是 `"aiGenerate"`

**排查方法**：字面量读不通时，回到 dex 原始字符串面（本轮导出 43.4 万条）或直接黑盒试错，
不要相信 jadx 中被折叠成单字母的字符串。

---

## 4. 端点全表

### 4.1 主服务 `sci.videomate.cc`（静态确认，部分已验证）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/common/{appName}/resource` | 素材/风格/模板目录总表（`appName="vibe"`）。**匿名可读，473 KB** |
| GET | `/common/{appName}/resourceNew` | 同上，新分页版 |
| GET | `/common/{appName}/getStyleTemplates` | AI 风格模板（本轮返回 `styleGroupList: null`，需更多上下文） |
| GET | `/storyart/ncrnau/beat/getResourceList` | 资源分页（`ver/pkg/page/pageSize/st/rt/lang/cc/packageLevel`）；`rt=4` 出 32 条故事模板 |
| GET | `/common/country/getCountryCode` | 国家码 |
| POST | `/billing/pre/general` | 预扣费探测（`userid/cp/version/platform` header） |
| POST | `/billing/property/userReport` | 用户属性上报（`/new/billing/...` 为 V2） |
| POST | `/billing/property/exchangeCode` | 兑换码 |
| POST | `/billing/link/installCall` | 安装归因 |
| POST | `/algo/v1/aiGenerate-aigc/multipleUpload` | **图床上传（multipart，字段 `files`）** |
| GET | `/v1/config`、`/v1/stories/app/view` | 配置/故事（后者 404，服务已下线） |

### 4.2 AI 云算法 `sci.videomate.cc/algo/v1/faceFusion/**`（静态确认，50+ 端点）

图像（本仓库主用）：

| 路径 | 用途 |
|---|---|
| `/algo/v1/faceFusion/createAiStyleTask` | **建风格任务（effectType 走这里，已验证）** |
| `/algo/v1/faceFusion/queryAiStyleTask` | **轮询（已验证）** |
| `/algo/v1/faceFusion/cancelAiStyleTask` | 取消 |
| `/algo/v1/aiGenerate-aigc/multipleUpload` | 上传（已验证） |
| `/algo/v1/faceFusion/aigc/image2image/createAiStyleTask` | 新版风格任务（要求 `token` 非空） |
| `/algo/v1/faceFusion/aigc/image2image/createImageTask` | 新版图生图（缺参报 `NumberFormatException: null`） |
| `/algo/v1/faceFusion/aigc/createInpaintAnything` / `createInpaintCleaner` | 局部重绘 / 去杂物 |
| `/algo/v1/faceFusion/aigc/createFaceKeyTask[V2]` + `queryFaceKeyTask[V2]` | 人脸关键点 |
| `/algo/v1/faceFusion/aigc/createAiPhotoModel[V2]` + `queryAiPhotoModels` | 个人写真模型训练 / 查询 |
| `/algo/v1/faceFusion/aigc/createAiPhotoTask[V2]` + `queryAiPhotoTask` | 写真任务 |
| `/algo/v1/faceFusion/aigc/createAiCustomizeTask` + `queryAiCustomizeTask` + `cancelAiCustomizeTask` | 自定义风格 |
| `/algo/v1/faceFusion/aigc/createSwapFace` | 换脸 |
| `/algo/v1/faceFusion/aigc/getFaceInfo` | 人脸属性 |
| `/algo/v1/faceFusion/aigc/createPictureDetectTask` + `queryPictureDetectTask` | 事前检测 |
| `/algo/v1/faceFusion/aigc/babyPredictCreate[V2]` + `query[V2]` | 宝宝预测 |
| `/algo/v1/faceFusion/aigc/livePortraitCreate` + `query` | LivePortrait 视频驱动 |
| `/algo/v1/faceFusion/aigc/emoVideoCreate` + `emoVideoQuery` + `emoVideoCancel` | 情绪视频 |
| `/algo/v1/faceFusion/aigc/stitchImage/createTask` + `queryTask` | 多图拼接（`ServerRequestManager.K/G`） |

视频（本轮未打通）：

`image2video/{createTask,queryTask,unifiedTaskCreate,integratedTaskCreate,image2VideoCreateV5}`、
`image2VideoCreate[V2/V3]`、`image2VideoQuery`、`image2VideoCancel`、`image2VideoDetect`、
`beatImage2Video/{createNormalTask,queryTask,cancelTask}`、`text2Video/{createTask,queryTask,cancelTask}`、
`text2video/unifiedTaskCreate`、`integrated/image2VideoCreateV4`、`createVideoHdTask[Vidu]`、
`queryVideoHdTask`、`cancelVideoHdTask`、`createVideoHdTaskVidu`、`image2VideoCustomer/{createTemplate,queryTemplate,cancelTemplate,upload}`、
`createSuperResolutionNew`、`querySuperResolutionNew`、`getAllPoseSequenceId`。

外挂（非 faceFusion 前缀）：

| 路径 | 用途 |
|---|---|
| `/algo/v1/pic/segment` | 抠图/分割 |
| `/algo/v1/faceAnimation/{createFaceAnimationTask,queryFaceAnimationTask,redoFaceAnimationTask,cancelFaceAnimationTask,multipleUpload}` | 人脸动画 |
| `/style/v1/face/faceSwap`、`/style/v1/videoFaceFusion`、`/style/v1/videoFaceDriven` 等 | 视频换脸/驱动 |
| `/file/style/upload`、`/file/style/multipleUpload` | 风格文件上传 |

---

## 5. 关键 DTO

### 5.1 统一信封（全域一致）

```json
{"c": 200, "m": null, "d": {...}, "traceId": "…"}
```

`c != 200` 即业务错：`1001`=参数错、`1003`=参数非法、`500`=内部异常、`3035`=走了错的任务类型。

### 5.2 上传

```
POST /algo/v1/aiGenerate-aigc/multipleUpload?ifHttps=true
Header: userid|userId, timeStamp, cp, version, platform, sign
Body  : multipart/form-data, 字段名 files（多个可重复），Content-Type: image/jpeg
→ {"c":200,"m":"成功","d":["https://algo.videomate.cc/original/261030/<uuid>.jpeg"]}
```

### 5.3 建任务

```json
POST /algo/v1/faceFusion/createAiStyleTask?ifWise=true
{
  "styleParams": {
    "style": "aiGenerate",
    "params": "{\"effectType\":\"2402\",\"width\":512,\"height\":512,\"oldPicture\":\"1\"}",
    "requestUrls": [{"index": 0, "imgUrl": "https://algo.videomate.cc/original/…jpeg"}]
  },
  "level": 0,
  "token": "",
  "width": 1080
}
```

- `params` 是**字符串化的内层 JSON**（双层编码，容易踩坑）
- `level` = 用户等级（`packageLevel`），`token` = 会员 token（非会员传 `""`）
- `width` = **屏幕宽度**（`AIGCServer` 从 `DisplayMetrics.widthPixels` 取），不是图片宽度
- 响应 `d`：`{jobId, jobStatus, responseUrls, jobReason, waitTime, async, maskUrl}`

内层 `params` 的形态由 effectType 家族决定，APK 里可见两种（静态确认）：

```java
// ce/a.java   EnhanceParams       // ce/b.java   ExpandParams
{"height":h,"width":w,            {"height":h,"width":w,
 "oldPicture":"1",                 "ratio":100,"effectType":"2433","mode":0}
 "effectType":"2402"}             // EXPAND_FREE_MODE 时额外带 "location":"l,t,r,b"
```

### 5.4 轮询

```
POST /algo/v1/faceFusion/queryAiStyleTask?ifWise=true   Body: {"jobId":"…"}
→ d: {jobId, jobStatus: doing|success|fail, responseUrls:[…], jobReason}
```

### 5.5 jobId 前缀 = 上游引擎名（已验证，逆向里最省事的探针）

| jobId 前缀 | effectType 样本 | 引擎语义 |
|---|---|---|
| `aiGenerate…` | 2402 | 超分/锐化（输出路径含 `aiImageResolution/imagesharpen`） |
| `PL_60016_IMG_HA-…` | 9999 | 写实生成 |
| `60019-…` | 2501 | `gx_img2img_speed_2511` img2img |
| `hairRepaint…` | 2701 | 染发（结果要走视频端点查） |
| `22-…` | 21xx/22xx/23xx | 卡通/动漫家族（缺参 → `fail/-1`） |
| `4-…` | 29xx | 未知家族（缺参 → `fail/-1`） |

### 5.6 effectType 家族（静态确认）

`com/gallery/preload/m.java:351` 用前缀白名单判定"云算法效果"：

```
2100 / 2200 / 2300 / 2400 / 2500 / 2700 / 2900
```

APK 里可见的具体 id：`2402`(enhance)、`2432`(expand free)、`2433`(expand proportion)、
`2100/2200/2300/2500/2700/2900`（家族头）。dex 里 2xxx 四位串仅 47 个，说明**风格库主要在服务端下发**，
客户端只有引擎路由。

---

## 6. 实测证据时间线（全部为真实请求）

| 时间(UTC) | 动作 | 结果 |
|---|---|---|
| 21:14 | `GET https://face-api.ufotosoft.com/` | 404 Spring 信封（误判 host） |
| 21:29 | `GET /common/vibe/resource` | 200，473 KB，17 组 202 条素材 |
| 21:31 | `POST /algo/v1/aiGenerate-aigc/multipleUpload`（字段名试错 5 次） | `files` 命中 → `https://algo.videomate.cc/original/261030/39962e8c-….jpeg` |
| 21:36 | `createAiStyleTask` effectType=2402 | `jobId=aiGenerate1793309781-…` → `success` → `imagesharpen-3-….jpg` **1024×1024 / 34604 B** |
| 21:37 | effectType 家族扫描（9999/2101/2102/2201/2202/2301/2501/2701/2901） | 9999、2501、2701 受理；21xx/22xx/23xx/29xx 立即 `fail reason=-1` |
| 21:38 | 查询 9999 任务 | `success` → `aiGenerate/….jpg` **768×1376 / 1 598 044 B** |
| 21:38 | 查询 2501 任务 | `success` → `gx_img2img_speed_2511-….jpeg` **792×1320 / 72 203 B** |
| 21:38 | 查询 2701 任务 | `c=3035 IMAGE2VIDEO_OTHER`（染发结果走视频端点） |
| 21:39 | 2501/9999 带 `prompt` 重跑 | 输出仍是同一套固定模板 → **prompt 被忽略** |
| 21:46 | 网关 real 模式 enhance | 200，11.11 s |
| 21:46 | 网关 real 模式 portrait | 200，13.98 s |
| 21:46 | 网关 real 模式 gen | 200，26.72 s |
| 21:50 | Playwright E2E（real, enhance） | PASS，9.28 s |

---

## 7. 坑清单（照抄会出事的地方）

1. **host 猜错**：`face-api.*` 不是 AI 入口，`sci.videomate.cc` 才是；且 retrofit base 不带尾斜杠也能工作，别自己补。
2. **签名单位**：必须秒级 `int(time.time())`；毫秒会 401/签名错。
3. **双层 JSON**：`styleParams.params` 是字符串，不是对象。
4. **`width` 语义**：外层 `width` 是屏幕宽（1080），图片尺寸在内层 `params` 里。
5. **multipart 字段名严格**：必须是 `files`，`file/img/image` 一律回 `param illegal: files is empty`。
6. **上传 Content-Type**：`image/jpeg`；本地图片若不是 JPEG 建议先转码。
7. **jadx 字符串折叠**：看到 `"n"` 就当未知，回 dex 字符串面确认。
8. **`ifWise=true`**：建任务/查询必须带（`b()` interceptor 会自动加，手工调用要显式加）。
9. **AIGC 新变体要 token**：`/aigc/image2image/createAiStyleTask` 文档 `token` 字段空会被拒（`param illegal: token`）。
10. **尺寸不受控**：`2402` 恒 1024×1024、`2501` 恒竖版；接网关时不要向调用方承诺尺寸。
11. **`getResourceList` 的 `rt` 参数**：只有 `rt=4/7/12` 有数据，其余 `totalCount=0`，别以为是权限问题。

---

## 8. 未解点 / 下一步

| 项 | 现状 | 下一步建议 |
|---|---|---|
| 2100/2200/2300/2900 家族 | 提交受理但 `fail(-1)`，缺 `gender/age/mask/modId` 之类 | 从 `LayerEditParam`/`ActionsItem` 反推必需参数；或真机抓一次成功的卡通请求 |
| 2701 染发 | `c=3035 IMAGE2VIDEO_OTHER` | 改用视频查询端点（`queryVideoHdTask` 等）轮询 |
| `aigc/image2image/createImageTask` | `NumberFormatException: null` | 补 `params` 内层数值字段（疑似 `strength/seed`） |
| `getStyleTemplates` | `styleGroupList: null` | 试 `country/language/abTest/st` 组合，或改用资源目录里的 `effectType` 空字段组 |
| 视频链路 | 未打通 | `image2video/createTask` 需要模板 id，先用 `getResourceList rt=4` 的模板 |
| 上游限流 | 未知上限 | 只做低样本探活；**不做上游压测** |
| prompt | 被忽略 | 若要真·文生图，得另找接口（如 `text2video` 或 AIGC 新变体） |
