# ComfyUI-GrsAI

GrsAI 图片与视频生成节点，支持提示词、参考素材和并发批量生成。

## 安装与配置

将仓库放入 `ComfyUI/custom_nodes/ComfyUI-GrsAI`，使用 ComfyUI 的 Python 安装依赖：

```sh
python -m pip install -r requirements.txt
```

重启 ComfyUI。节点的 `apikey` 可以直接填写密钥；留空时读取环境变量或插件目录 `.env`：

```dotenv
GRSAI_API_KEY=your_api_key_here
GRSAI_BASE_URL=https://grsai.dakka.com.cn
```

默认国内接口为 `https://grsai.dakka.com.cn`，全球接口为 `https://grsaiapi.com`。密钥可在 [GrsAI 控制台](https://grsai.ai/zh/dashboard/api-keys) 获取。价格以平台控制台为准。

## 节点与模型

| 节点 | 模型 |
| --- | --- |
| 🎨 GrsAI GPT Image | `gpt-image-2`、`gpt-image-2-vip`、`gpt-image-2.5`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst` |
| 🍌 GrsAI Nano Banana - Text/Image | `nano-banana`、`nano-banana-fast` |
| 🍌 GrsAI Nano Banana 2 - Text/Image | `nano-banana-2`、`nano-banana-2-cl`、`nano-banana-2-2k-cl`、`nano-banana-2-4k-cl` |
| 🍌 GrsAI Nano Banana Pro - Text/Image | `nano-banana-pro`、`nano-banana-pro-vt`、`nano-banana-pro-cl`、`nano-banana-pro-vip`、`nano-banana-pro-4k-vip` |
| 🎬 GrsAI MiniMax H3 - Text/Image/Audio | `minimax-h3` |

现有图片节点保持原注册名称、参考图端口和 `IMAGE / STRING` 输出。无参考图时文生图，有参考图时图生图。`num_images` 控制独立生成请求数，支持1–12；并发数量越大，越容易遇到平台限流。

### GPT Image

`aspect_ratio` 包含文档的标准与 VIP 尺寸选项；`custom_size` 非空时覆盖下拉选项。不同模型的尺寸规则不同，非法组合会在请求前报错。

| 模型 | 尺寸 | quality | transparent |
| --- | --- | --- | --- |
| gpt-image-2 / gpt-image-2.5 | 比例或文档列出的1K像素值 | auto | 不支持 |
| gpt-image-2-vip | 1–4K像素值 | medium | 支持 |
| gpt-image-2.5-flare | 1–4K像素值 | low / medium / high | 支持 |
| gpt-image-2.5-sunburst | 1–4K像素值 | low / medium / high / xhigh / max | 支持 |

`quality=default`、`background=default` 表示省略该可选参数，由服务端决定默认行为；这两个 `default` 值不会发给 API。`background=transparent` 会保留返回图片的透明通道。

VIP、flare、sunburst 的自定义像素尺寸：最大边不超过3840，两边均为16的倍数，长短边比例不超过3:1，总像素为655360–8294400。`auto` 按文档作为例外选项保留。

### Nano Banana

`image_size` 支持 `1K / 2K / 4K`。新版文档没有为此字段限制模型，所有 Nano Banana 模型均按选择传递。

基础比例：`auto / 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / 3:2 / 2:3 / 5:4 / 4:5 / 21:9`。

Nano Banana 2 系列额外支持 `1:4 / 4:1 / 1:8 / 8:1`。旧工作流中的 `nano-banana-2-cl-4k` 请改选 `nano-banana-2-4k-cl`；Python 客户端兼容旧名称并映射到正确名称。

### MiniMax H3

沿用现有节点的提示词、密钥、模型选择、可选参考素材、并发和状态返回结构。视频输出使用 ComfyUI 原生 `VIDEO` 类型，连接 `Save Video` 即可保存；批量结果会逐个传给下游。需要提供 `comfy_api.input_impl.VideoFromFile` 的 ComfyUI 版本。

- `aspect_ratio`：`portrait / landscape / square`。
- `resolution`：`480p / 768p / 1080p`。
- `duration`：1–15秒，1080p最多10秒。
- `seed`：原样传递所选整数，0也作为0传递。
- `image_1`–`image_9`：图片输入；总参考图片最多9张。
- `audio_1`–`audio_3`：ComfyUI `AUDIO` 输入，转换为 WAV base64；总参考音频最多3段。
- `images`、`audios`：可填写 URL 或 base64 的 JSON 数组，与连接的素材合并计算数量，默认 `[]`。
- `num_videos`：1–12个独立生成请求。

成功视频立即下载到 `ComfyUI/output/GrsAI`，验证可读取后返回 `VIDEO`。下载重试耗尽时，状态文本保留原链接，避免重新付费生成。

## 请求与容错

图片与视频模型均使用 `POST /v1/api/generate`，参考图片字段为 `images`。客户端原有 Python 参数 `urls` 保留兼容，发送时转换为 `images`。

- `reply_type=async` 为默认值，提交后使用 `GET /v1/api/result?id=...` 查询。
- 也支持 `json` 和 `stream`，节点最终仍返回完整图片或视频。SSE 事件会解析成最终任务结果。
- 查询遇到连接异常、429或5xx时有界退避；生成 POST 不自动重试，避免重复生成计费。
- `failed`、`violation` 为终态，HTTP 400 中的任务错误也会读取。
- 默认单次请求超时300秒、总生成等待上限1800秒、查询间隔3秒；通过 `GrsaiConfig` 的 `timeout`、`generation_timeout`、`poll_interval` 配置。
- 下载最多尝试3次；部分成功仍返回成功素材和失败详情。下载请求不带 API 密钥。
- 生成超时保留任务 ID，可通过 `GrsaiAPI.get_result(task_id)` 查询原任务。

旧 Flux 客户端保留兼容代码；新版文档未列出 Flux 模型，因此未将其迁移到新接口。OpenAI/Gemini 兼容端点是同平台的其他接入格式，不另造对话节点；本插件的生成模型使用文档自有生成接口。

## 验证

使用安装了 ComfyUI 依赖的 Python 运行离线测试，无需 API 密钥：

```sh
python -X utf8 test_api_contract.py
```

覆盖模型与参数矩阵、异步轮询、限流、断连、SSE、错误响应、部分下载、节点传参、透明度和视频读写。模拟测试验证本地实现，不能证明平台所有模型的实时可用性。仓库其他旧测试脚本可能调用付费 API。

## 文档来源

2026-09-15 核对的接口规范：

- [Nano Banana](https://qmy27nhsd9.apifox.cn/452392911e0)
- [GPT Image 2 / 2.5](https://qmy27nhsd9.apifox.cn/452409160e0)
- [MiniMax H3](https://qmy27nhsd9.apifox.cn/514679297e0)
- [异步查询](https://qmy27nhsd9.apifox.cn/452409577e0)

## 许可证

[MIT](LICENSE)
