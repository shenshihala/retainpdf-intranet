# OCR Provider 列表

## 接口

```http
GET /api/v1/providers/ocr
```

用于发现后端当前支持的 OCR provider、凭据字段、可配置 options、能力和产物布局。

## 响应示例

```json
{
  "code": 0,
  "message": "ok",
  "data": [
    {
      "key": "paddle",
      "display_name": "PaddleOCR",
      "provider_kind": "remote",
      "credential": {
        "field": "paddle_token",
        "env": "RETAIN_PADDLE_API_TOKEN",
        "required_for": ["remote_url", "local_upload"]
      },
      "options": {
        "paddle_model": {
          "type": "string",
          "default": "PaddleOCR-VL-1.6",
          "aliases": {
            "paddleocr-vl": "PaddleOCR-VL-1.6"
          }
        }
      },
      "capabilities": {
        "supports_remote_url_submit": true,
        "supports_local_file_upload": true,
        "supports_polling": true,
        "supports_download_bundle": true,
        "supports_extra_formats": false,
        "supports_formula_toggle": false,
        "supports_table_toggle": false
      },
      "artifact_layout": {
        "provider_result_json": "paddle_result.json",
        "provider_bundle_zip": "paddle_bundle.zip",
        "provider_raw_dir": "paddle_raw",
        "layout_json": "paddle_result.json"
      }
    }
  ]
}
```

## 内网 PaddleOCR-VL

配置型 provider：`intranet_paddle`（`local_command` + wrapper → 同步 `/layout-parsing`）。  
详见 [06-intranet-paddle.md](./06-intranet-paddle.md)。云端 `paddle`（异步 AI Studio）行为不变。

## 内网 MinerU

配置型 provider：`intranet_mineru`（`local_command` + wrapper → 同步 `/file_parse`）。  
详见 [07-intranet-mineru.md](./07-intranet-mineru.md)。云端 `mineru`（异步 `/api/v4` + zip）行为不变。

## provider_kind

- `remote`: 后端内置远程 provider，例如 MinerU、Paddle。
- `local_command`: 配置型本地命令 provider。
- `remote_command`: 配置型远程命令 provider。

## 前端规则

- 不要硬编码 provider 参数表。
- 表单字段从 `credential` 和 `options` 生成。
- `credential` 为 `null` 时不展示凭据输入。
- provider-specific 非密钥参数写入 `ocr.options`。
