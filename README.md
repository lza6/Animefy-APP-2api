# Animefy 2API —— 逆向分析与 OpenAI 兼容网关

目标 APK：`Animefy v2.37.10186 (Premium).apk`
包名 `com.picastory.ins.editor`（ufotosoft / vibe 白标家族，与 videomate.cc 同源）· versionCode `10186` · minSdk 21 / targetSdk 36

本仓库是 APK 静态逆向 + 线上黑盒验证的落地产物：协议客户端、OpenAI 兼容网关、demo 页、压测脚本、以及完整逆向报告。

---

## 一句话结论

上游 AI 云算法后端 **`https://sci.videomate.cc`** 的 `/algo/v1/faceFusion/**` 接口族，鉴权是「客户端自造 userid + `md5(signKey+秒级时间戳)`」，**signKey 可从 APK 内 DES 字面量还原**，服务端不校验账号存在性 —— 因此**全链路匿名可跑通**，无需注册、无需真机、无需证书固定绕过。

## 能力矩阵（都经过真实出图验证）

| 对外模型名 | effectType | 上游引擎（jobId 前缀） | 实测行为 | 输出尺寸 | 状态 |
|---|---|---|---|---|---|
| `animefy-enhance` | `2402` | `aiGenerate…` / `aiImageResolution/imagesharpen` | 锐化 + 超分 | 1024×1024 | ✅ 已验证 |
| `animefy-gen` | `9999` | `PL_60016_IMG_HA` | 写实向 AI 生成 | 768×1376（1.5 MB） | ✅ 已验证 |
| `animefy-portrait` | `2501` | `60019…` / `gx_img2img_speed_2511` | 人像写真 img2img | 792×1320（忽略请求尺寸） | ✅ 已验证 |
| — | `2101/2201/2301/2901` | `22…` / `4…` | 提交受理但 `jobStatus=fail, reason=-1` | — | ⚠️ 缺参数 |
| — | `2701` | `hairRepaint` | 染发，走视频查询端点（`c=3035 IMAGE2VIDEO_OTHER`） | — | ⚠️ 待打通 |

> 上游 **忽略自由文本 prompt**：同样的"苹果/蓝猫" prompt 与无 prompt 的输出是同一套固定风格模板。这是「风格 API」而不是「通用文生图」，接 new-api 时不要按文生图渠道定价。

## 快速开始

```powershell
pip install -r requirements.txt

# 1) 离线自测（不打上游，mock 变换）
$env:ANIMEFY_MODE="mock"; python demo_server.py --port 8931
# 2) 真打上游（需能访问 sci.videomate.cc，本机走代理）
$env:ANIMEFY_MODE="real"; $env:ANIMEFY_PROXY="http://127.0.0.1:10808"; python demo_server.py --port 8937
```

浏览器打开 `http://127.0.0.1:8931/` 即是 demo 页。

```bash
# 压测（只压本地网关）
python stress_test.py --requests 120 --steps 1,5,10,20,40
# 真实链路小样本
python stress_test.py --url http://127.0.0.1:8937 --requests 6 --steps 1,3
# 浏览器 E2E + 截图
python verify_demo.py --url http://127.0.0.1:8931 --model animefy-portrait
```

## 目录

```
2api/
├── animefy_client.py      协议客户端（签名/上传/建任务/轮询/下载）
├── demo_server.py         OpenAI 兼容网关（mock / real 双模，并发闸门）
├── static/demo.html       demo 页（上传→生成→对比）
├── stress_test.py         本地压测（阶梯并发、p50/p95/p99）
├── verify_demo.py         Playwright 真实浏览器 E2E
├── docs/REVERSE-REPORT.md 逆向深度报告（端点全表 / DTO / 证据链）
└── docs/NEW-API-INTEGRATION.md  交给另一个会话执行的对接文档
```

## 本轮验证结果（实际运行，非推断）

| 项目 | 命令 | 结果 |
|---|---|---|
| mock 阶梯压测 | `stress_test.py --requests 120 --steps 1,5,10,20,40` | **600/600 成功，0 失败**；峰值 56.5 rps@c=10，c=40 时 p50 825ms |
| real 小样本压测 | `--url :8937 --requests 6 --steps 1,3` | **12/12 成功**；c=1 p50 6192ms，c=3 p50 6045ms / p95 11925ms |
| demo 页 E2E（mock） | `verify_demo.py --model animefy-portrait` | **PASS**，截图 `browser_demo_animefy-portrait.png` |
| demo 页 E2E（real） | `verify_demo.py --url :8937 --model animefy-enhance` | **PASS**，端到端 9.28s |
| 真实出图 | `POST /v1/images/edits` ×3 模型 | enhance 11.1s / portrait 14.0s / gen 26.7s，输出见 `static/out/` |

## 已知限制

- 上游只接受 `image`（图生图链路）：网关的 `/v1/images/generations` 也必须带图，没有纯文生图。
- 尺寸不完全受控：`2402` 恒定 1024×1024，`2501` 恒定竖版，只有 `9999` 跟随请求尺寸。
- 2100/2200/2300/2900 家族缺参数（疑似需 `gender/age/mask/modId` 之类），本轮未攻破。
- 未测上游账号级限流上限；**不要对上游做压测**，压测只打本地网关。

---

## 接口速查（给 new-api 对接方）

| 端点 | 说明 |
|---|---|
| `GET /v1/models` | 返回 3 个已验证模型 |
| `GET /healthz` | mode / 计数 / inflight |
| `GET /animefy/effects` | effectType 注册表（含 `verified` 标记） |
| `POST /v1/images/edits` | multipart：`file` + `model` + `size` + `response_format`（**推荐**） |
| `POST /v1/images/generations` | JSON，**必须带 `image`**（b64 / dataURL / http URL） |
| `POST /animefy/raw` | 研究用直通：任意 `effectType`，不经白名单 |

```bash
curl -X POST http://127.0.0.1:8937/v1/images/edits \
  -F "file=@input.jpg" -F "model=animefy-enhance" \
  -F "size=1024x1024" -F "response_format=url"
# {"created":…,"data":[{"url":"/static/out/….jpg"}],
#  "animefy":{"model":"animefy-enhance","effectType":"2402","jobId":"aiGenerate…","elapsed":8.99,"mode":"real"}}
```

完整对接步骤（含 new-api 原生渠道 type=80 的改动点、定价、排错表）见
**[docs/NEW-API-INTEGRATION.md](docs/NEW-API-INTEGRATION.md)**。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANIMEFY_MODE` | `auto` | `real` / `mock` / `auto` |
| `ANIMEFY_PROXY` | 空 | 上游需代理时填，如 `http://127.0.0.1:10808` |
| `ANIMEFY_USER_ID` | 随机 20 位 | 想固定 userid 时设置 |
| `MAX_CONCURRENCY` | `4` | 并发闸门（上游并发上限未测，建议 ≤4） |
| `REQUEST_TIMEOUT` | `300` | 单任务超时（秒） |
| `MAX_SIDE` | `1536` | 尺寸上限 |
| `MAX_IMAGE_BYTES` | `20971520` | 输入图上限 |

## 免责声明

本项目仅用于**协议研究与互操作性验证**：分析对象为公开发布的 Android 安装包，所有结论来自静态反编译
与低频黑盒请求（未做上游压测）。代码按 MIT 发布，**不提供任何可用性/合法性担保**，请在遵守目标服务
条款与所在地法律的前提下使用。
