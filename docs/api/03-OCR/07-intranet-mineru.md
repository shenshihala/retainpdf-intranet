# 内网 MinerU（intranet_mineru）

方案 A：用已有 `local_command` 插件，通过仓库内 wrapper 调用自托管 **mineru-api** 的同步接口 `POST /file_parse`。

**不走**云端 MinerU 异步 `/api/v4/extract/task` + zip bundle。内置云端 `mineru` provider 保持不变。

## 何时用

- 内网已部署官方 / 社区常见的 `mineru-api`（FastAPI，`POST /file_parse`，常见端口 `30000`）
- 希望 RetainPDF 继续用内置 **mineru** adapter（legacy `middle.json`：`pdf_info` + `para_blocks`）
- 暂时不想改 Rust mineru client / 云端任务机

## 目标 API（主路径）

```text
POST {BASE}/file_parse
Content-Type: multipart/form-data

files=@document.pdf
return_middle_json=true
return_md=false
backend=pipeline|vlm-auto-engine|...
lang_list=ch
formula_enable=true|false
table_enable=true|false
```

响应信封（示意）：

```json
{
  "backend": "pipeline",
  "version": "2.x",
  "results": {
    "document": {
      "middle_json": "{ ... pdf_info ... }"
    }
  }
}
```

wrapper 会抽出 `middle_json`（字符串或对象），写成 local_command 的 `RETAIN_OCR_RAW_PAYLOAD_JSON`，由 `raw_provider=mineru` 走内置 adapter。

### 为何选 `/file_parse` 而不是 MinerU 4.0 V1

| API | 输出 | 与 RetainPDF mineru adapter |
| --- | --- | --- |
| `mineru-api` `POST /file_parse` | legacy `*_middle.json`（`pdf_info`） | **直接兼容**（本方案） |
| 云端 `mineru.net` `/api/v4/...` | zip 内 legacy middle | 云端内置路径，本方案不碰 |
| MinerU 4.0 `POST /v1/file_parse` | `docvortex.middle`（`schema`/`pages`/`blocks`） | **尚未适配**；遇到会明确报错 |

URL / 超时 / token 均可配置；若日后要接 V1，需要单独的 docvortex→document.v1 适配，而不是 silently 改云端 mineru。

## Provider 配置

`backend/config/ocr_providers.json` 已注册：

```json
"intranet_mineru": {
  "display_name": "Intranet MinerU",
  "kind": "local_command",
  "credential": null,
  "options": {
    "command": {
      "type": "string",
      "default": "python -m retainpdf_pipeline.ocr.ocr_provider.intranet_mineru_wrapper"
    },
    "raw_provider": {
      "type": "string",
      "default": "mineru"
    }
  }
}
```

要点：

- `kind: local_command` → OCR 阶段跑 wrapper，不走云端异步任务机
- `raw_provider: mineru` → raw JSON 交给内置 mineru adapter（不要改成 `generic_flat_ocr`，除非你接受语义降级）

建任务：

```json
{
  "ocr": {
    "provider": "intranet_mineru"
  }
}
```

## Wrapper 环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `RETAIN_INTRANET_MINERU_BASE_URL` | 是（非 dry-run） | 自托管 mineru-api base，如 `http://192.168.1.10:30000`；也可直接写到 `/file_parse` |
| `RETAIN_INTRANET_MINERU_TOKEN` | 否 | 网关鉴权 token；官方 mineru-api 通常无鉴权 |
| `RETAIN_OCR_CREDENTIAL` | 否 | local_command 注入的凭据；token 未设时作为回退 |
| `RETAIN_INTRANET_MINERU_AUTH_SCHEME` | 否 | `bearer`（默认）/ `token` / `none` / `raw` |
| `RETAIN_INTRANET_MINERU_TIMEOUT_SECONDS` | 否 | HTTP 超时，默认 `1800` |
| `RETAIN_INTRANET_MINERU_BACKEND` | 否 | mineru-api `backend`；默认 `pipeline`。`vlm` 会映射为 `vlm-auto-engine` |
| `RETAIN_INTRANET_MINERU_LANGUAGE` | 否 | `lang_list`，默认 `ch` |
| `RETAIN_INTRANET_MINERU_DISABLE_FORMULA` | 否 | `1`/`true` → `formula_enable=false` |
| `RETAIN_INTRANET_MINERU_DISABLE_TABLE` | 否 | `1`/`true` → `table_enable=false` |
| `RETAIN_INTRANET_MINERU_OPTIONAL_JSON` | 否 | 额外 form 字段 JSON 对象（白名单过滤） |
| `RETAIN_INTRANET_MINERU_DRY_RUN` | 否 | `1`/`true` 时不调 HTTP，写合成 mineru payload 便于接线验证 |
| `RETAIN_LOCAL_OCR_COMMAND_TIMEOUT_SECONDS` | 否 | local_command 进程超时，默认 `1800`；大 PDF / GPU 慢时请加大 |

local_command 约定输入（由 RetainPDF 注入，wrapper 读取）：

- `RETAIN_OCR_SOURCE_PDF`
- `RETAIN_OCR_RAW_PAYLOAD_JSON`

## 与云端 mineru / 翻译的关系

| 能力 | 配置 | 说明 |
| --- | --- | --- |
| 本方案 OCR | `ocr.provider=intranet_mineru` + `RETAIN_INTRANET_MINERU_BASE_URL` | wrapper 自己发 HTTP；**不依赖**云端 token |
| 云端 mineru | `ocr.provider=mineru` + `RETAIN_MINERU_API_TOKEN` | 仍走 `/api/v4` 异步 + zip；勿把内网 URL 塞进云端路径 |
| 私网探测放行 | `RUST_API_ALLOW_PRIVATE_PROVIDER_URLS=true` | 主要影响 provider probe / 部分 URL 校验；本 wrapper 直连内网通常仍建议打开 |
| 内网翻译 LLM | `translation.base_url` + `model` + `api_key` + `api_protocol` | 与 OCR 正交 |

示例环境：

```bash
export RETAIN_INTRANET_MINERU_BASE_URL=http://mineru.intranet:30000
export RETAIN_INTRANET_MINERU_BACKEND=pipeline
export RUST_API_ALLOW_PRIVATE_PROVIDER_URLS=true
```

## 限制（必读）

1. **同步阻塞**：`/file_parse` 会占满一个 OCR worker，直到返回；大文档请加 timeout。
2. **legacy middle.json**：本方案依赖 `pdf_info`；MinerU 4.0 V1 的 `docvortex.middle` 会失败并提示，而不是悄悄降级。
3. **不要**开 `response_format_zip`：local_command 契约写单文件 JSON；wrapper 会强制关掉该选项。
4. **不要**把本方案和云端 `mineru` 混用 base_url：协议不同。
5. **图片资源**：当前只写入 middle JSON；若需要图床式 asset 回灌，后续可扩展 zip 旁路（非本提交范围）。

## 手动 dry-run

```bash
export RETAIN_OCR_SOURCE_PDF=/path/to/sample.pdf
export RETAIN_OCR_RAW_PAYLOAD_JSON=/tmp/intranet_mineru_raw.json
export RETAIN_INTRANET_MINERU_DRY_RUN=1
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_mineru_wrapper
# 或
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_mineru_wrapper --dry-run \
  --source-pdf /path/to/sample.pdf \
  --raw-payload-json /tmp/intranet_mineru_raw.json
```

对真实内网服务：

```bash
export RETAIN_INTRANET_MINERU_BASE_URL=http://127.0.0.1:30000
unset RETAIN_INTRANET_MINERU_DRY_RUN
# 先用 curl 确认 /docs 或 /health
python -m retainpdf_pipeline.ocr.ocr_provider.intranet_mineru_wrapper \
  --source-pdf /path/to/sample.pdf \
  --raw-payload-json /tmp/intranet_mineru_raw.json
```

## 相关文档

- [local_command 插件](./04-local-command插件.md)
- [OCR Provider 列表](./01-provider列表.md)
- [内网 PaddleOCR-VL](./06-intranet-paddle.md)
