# 实证记录（本目录所有结论的可复现依据）

## 1. 探针矩阵（真实请求，逐步收敛）

| # | 请求 | 结果 | 结论 |
|---|---|---|---|
| 1 | `GET https://face-api.ufotosoft.com/algo/v1/faceFusion/aigc/queryAiPhotoModels` | 404 Spring 信封 | 该 host 不是 AI 入口 |
| 2 | `GET https://face-api.videomate.cc/algo/...` | 404（`stgw`） | 同上，且被腾讯网关挡 |
| 3 | `POST https://sci.videomate.cc/algo/v1/faceFusion/aigc/queryAiPhotoModels` | `200 {"c":1003,"m":"param error: loraId is illegal"}` | **签名/头正确，服务端认了** |
| 4 | 上传字段名试错：`files` / `file` / `img` / `image` / `images` | 仅 `files` 成功 | 字段名严格 |
| 5 | `createAiStyleTask` effectType=2402 | `jobId=aiGenerate…` → success | 图像链路通 |
| 6 | effectType 家族扫描 | 见下表 | 引擎路由规律 |
| 7 | 带 `prompt` 重跑 9999/2501 | 同一套固定模板输出 | prompt 被忽略 |
| 8 | 网关 real 模式三模型 | 3/3 成功 | 端到端闭环 |

## 2. effectType 路由实测

| effectType | jobId 前缀 | 提交 | 结果 |
|---|---|---|---|
| `2402` | `aiGenerate1793309781-…` | 200 | **success**，`aiImageResolution/imagesharpen-3-….jpg` 1024×1024 / 34604 B |
| `9999` | `PL_60016_IMG_HA-…` | 200 | **success**，`aiGenerate/….jpg` 768×1376 / 1 598 044 B |
| `2501` | `60019-…` | 200 | **success**，`gx_img2img_speed_2511-….jpeg` 792×1320 / 72 203 B |
| `2701` | `hairRepaint…` | 200 | 查询回 `c=3035 IMAGE2VIDEO_OTHER`（视频端点） |
| `2101/2102` | `22-…` | 200 | `jobStatus=fail, jobReason=-1` |
| `2201/2202` | `22-…` | 200 | `jobStatus=fail, jobReason=-1` |
| `2301` | `22-…` | 200 | `jobStatus=fail, jobReason=-1` |
| `2901` | `4-…` | 200 | `jobStatus=fail, jobReason=-1` |
| `9999`（无 prompt） | `PL_60016_IMG_HA-…` | 200 | success（与带 prompt 同风格） |

## 3. 产物

| 文件 | 说明 |
|---|---|
| `proof_2402_enhance_1024x1024.jpg` | 2402 首张真实出图 |
| `proof_9999_gen_768x1376.jpg` | 9999 真实出图（1.5 MB） |
| `proof_2501_portrait_792x1320.jpg` | 2501 真实出图 |
| `endpoints.txt` | 从 dex/jadx 抽出的端点全表 |
| `resource_groups.txt` | `/common/vibe/resource` 的 17 个分组 |
| `resource_items_202.txt` | 202 条素材（含 resId/resTypeId/名称/包地址） |

## 4. 复现命令

```bash
# 只看资源目录（不烧生成额度）
python animefy_client.py

# 一次性真实出图（约 10-30s，会消耗上游额度）
python - <<'PY'
from animefy_client import AnimefyClient
from PIL import Image; import io
img=Image.new("RGB",(512,512),(200,150,120)); b=io.BytesIO(); img.save(b,"JPEG")
data, task = AnimefyClient(proxy="http://127.0.0.1:10808").generate(b.getvalue(), "2402")
open("out.jpg","wb").write(data); print(task.job_id, len(data))
PY
```
