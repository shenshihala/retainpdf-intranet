# 内网 PaddleOCR-VL（intranet_paddle）

方案 A：用已有 `local_command` 插件，通过仓库内 wrapper 调用自托管 PaddleOCR-VL 的同步接口 `POST /layout-parsing`。

**不走**百度 AI Studio 异步 `/api/v2/ocr/jobs`。内置云端 `paddle` provider 保持不变。

## 何时用

- 内网已部署官方 PaddleOCR-VL Docker / Compose（常见端口 `8080`）
- 希望 RetainPDF 继续用 paddle adapter 保留版面语义（`layoutParsingResults` + `dataInfo`）
- 暂时不想改 Rust paddle client

## Provider 配置

`backend/config/ocr_providers.json` 已注册：

```json
"intranet_paddle": {
  "display_name": "Intranet PaddleOCR-VL",
  "kind": "local_command",
  "credential": null,
  "options": {
    "command": {
      "type": "string",
      "default": "python -m retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper"
    },
    "raw_provider": {
      "type": "string",
      "default": "paddle"
    }
  }
}
```

关键点：

- `kind: local_command` → OCR 阶段跑 wrapper，不走云端异步任务机
- `raw_provider: paddle` → raw JSON 交给内置 paddle adapter，保留版面语义（不要改成 `generic_flat_ocr`，除非你接受语义降级）

建任务：

```json
{
  "ocr": {
    "provider": "intranet_paddle"
  }
}
```

可选覆盖 command：

```json
{
  "ocr": {
    "provider": "intranet_paddle",
    "options": {
      "command": "python -m retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper"
    }
  }
}
```

## Wrapper 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `RETAIN_INTRANET_PADDLE_BASE_URL` | 是（非 dry-run） | 自托管 OCR base，如 `http://192.168.1.10:8080`；也可直接写到 `/layout-parsing` |
| `RETAIN_INTRANET_PADDLE_TOKEN` | 否 | 网关鉴权 token；官方 compose 通常无鉴权 |
| `RETAIN_OCR_CREDENTIAL` | 否 | local_command 注入的凭据；token 未设时作为回退 |
| `RETAIN_INTRANET_PADDLE_AUTH_SCHEME` | 否 | `bearer`（默认）/ `token` / `none` / `raw` |
| `RETAIN_INTRANET_PADDLE_TIMEOUT_SECONDS` | 否 | HTTP 超时，默认 `1800` |
| `RETAIN_INTRANET_PADDLE_OPTIONAL_JSON` | 否 | 额外 optional 字段 JSON 对象（白名单过滤） |
| `RETAIN_INTRANET_PADDLE_DRY_RUN` | 否 | `1`/`true` 时不调 HTTP，写合成 paddle payload 便于接线验证 |
| `RETAIN_LOCAL_OCR_COMMAND_TIMEOUT_SECONDS` | 否 | local_command 进程超时，默认 `1800`；大 PDF / GPU 慢时请加大 |

local_command 约定输入（由 RetainPDF 注入，wrapper 读取）：

- `RETAIN_OCR_SOURCE_PDF`
- `RETAIN_OCR_RAW_PAYLOAD_JSON`

## 与私网 URL / 翻译的关系

| 能力 | 配置 | 说明 |
| --- | --- | --- |
| 本方案 OCR | `ocr.provider=intranet_paddle` + `RETAIN_INTRANET_PADDLE_BASE_URL` | wrapper 自己发 HTTP；**不依赖** `ocr.paddle_api_url` |
| 云端 paddle | `ocr.provider=paddle` | 仍走 AI Studio 异步协议；勿把内网 URL 塞进 `paddle_api_url` 指望直接通 |
| 私网探测放行 | `RUST_API_ALLOW_PRIVATE_PROVIDER_URLS=true` | 主要影响 provider probe / 部分 URL 校验（含翻译私网 endpoint）；本 wrapper 直连内网 OCR 通常仍建议打开，避免其它链路被拦 |
| 内网翻译 LLM | `translation.base_url` + `model` + `api_key` + `api_protocol`（常用 `openai`） | 与 OCR 正交；详见建任务契约 |

示例环境：

```bash
export RETAIN_INTRANET_PADDLE_BASE_URL=http://ocr.intranet:8080
export RUST_API_ALLOW_PRIVATE_PROVIDER_URLS=true
# 可选：翻译也走内网
export RETAINPDF_TRANSLATION_BASE_URL=http://llm.intranet:8000/v1
export RETAINPDF_TRANSLATION_MODEL=your-model
export RETAINPDF_TRANSLATION_API_KEY=YOUR_TRANSLATION_API_KEY
export RETAINPDF_TRANSLATION_PROTOCOL=openai
```

## 限制（必读）

1. **同步阻塞**：`/layout-parsing` 会占满一个 OCR worker，直到返回；大文档请加 timeout。
2. **GPU / 显存**：VL + vLLM 通常需要较高显存；CPU 路径很慢，仅适合验证。
3. **页数**：自托管默认可能限制输入页数（常见需在 Serving 配置里放开 `max_num_input_imgs`）；与 RetainPDF 的 `RUST_API_PADDLE_MAX_PAGES` 不是同一条路径，但业务上仍应对齐。
4. **不要**把本方案和云端 `paddle` 混用 base_url：协议不同。
5. **私网 SSRF**：打开 `RUST_API_ALLOW_PRIVATE_PROVIDER_URLS` 后，应限制谁能改 `translation.base_url` 等用户可控 URL。

## 手动 dry-run

```bash
export RETAIN_OCR_SOURCE_PDF=/path/to/sample.pdf
export RETAIN_OCR_RAW_PAYLOAD_JSON=/tmp/intranet_paddle_raw.json
export RETAIN_INTRANET_PADDLE_DRY_RUN=1
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper
# 或
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper --dry-run \
  --source-pdf /path/to/sample.pdf \
  --raw-payload-json /tmp/intranet_paddle_raw.json
```

对真实内网服务：

```bash
export RETAIN_INTRANET_PADDLE_BASE_URL=http://127.0.0.1:8080
unset RETAIN_INTRANET_PADDLE_DRY_RUN
# 先用 curl 确认：
# curl -s http://127.0.0.1:8080/health
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_paddle_layout_wrapper \
  --source-pdf /path/to/sample.pdf \
  --raw-payload-json /tmp/intranet_paddle_raw.json
```

## 相关文档

- [local_command 插件](./04-local-command插件.md)
- [OCR Provider 列表](./01-provider列表.md)
