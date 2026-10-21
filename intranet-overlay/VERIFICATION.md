# 核对清单

下面的命令假设配置服务在 `http://127.0.0.1:8080`，密钥与 `docker/auth.local.json` 里的 `YOUR_PROXY_API_KEY` 一致。经 nginx 时把主机改成站点源（overlay 默认 `http://127.0.0.1:8080`），浏览器不带 `X-API-Key` 时由 `RETAINPDF_PROXY_API_KEY` 补上。

准备一个空数据目录，并带上与 `app.env` 相同的回退变量：

```bash
export DEMO_ROOT=/tmp/intranet-ocr-demo
mkdir -p "$DEMO_ROOT/secrets"
export RETAIN_INTRANET_OCR_CONFIG_BIND=127.0.0.1
export RETAIN_INTRANET_OCR_CONFIG_PORT=8080
export RETAIN_INTRANET_OCR_CONFIG_PATH="$DEMO_ROOT/secrets/intranet-ocr-config.json"
export RETAIN_INTRANET_OCR_CONFIG_API_KEYS=YOUR_PROXY_API_KEY
export RETAIN_INTRANET_PADDLE_BASE_URL=http://host.docker.internal:8080
export RETAIN_INTRANET_PADDLE_AUTH_SCHEME=none
export RETAIN_INTRANET_MINERU_BASE_URL=http://host.docker.internal:8081
export RETAIN_INTRANET_MINERU_TOKEN=env-fallback-token-demo
export RETAIN_INTRANET_MINERU_AUTH_SCHEME=bearer
export RETAIN_INTRANET_MINERU_API=v1
export RETAIN_INTRANET_MINERU_TIER=basic
python3 ocr_provider/intranet_ocr_config_server.py
```

## GET：掩码，不回明文

```bash
curl -sS http://127.0.0.1:8080/api/v1/intranet/ocr-config \
  -H "X-API-Key: YOUR_PROXY_API_KEY" | python3 -m json.tool
```

期望：

- HTTP 200，`code` 为 `OK`
- `data.revision` 为 `0`（还没保存过）
- `providers.intranet_paddle.base_url` 为环境变量里的地址，`base_url_source` 为 `env`
- `providers.intranet_mineru.token_configured` 为 `true`，`token_source` 为 `env`
- 响应正文里**没有** `env-fallback-token-demo`
- 不带 `X-API-Key` 时为 401

## PUT：写入地址，空密钥表示保留

```bash
curl -sS -X PUT http://127.0.0.1:8080/api/v1/intranet/ocr-config \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "expected_revision": 0,
    "intranet_paddle": {
      "base_url": "http://ocr.example.local:8080",
      "auth_scheme": "bearer",
      "token": "paddle-ui-token-demo"
    }
  }'
```

再 GET 一次。期望 `revision` 为 `1`，Paddle 地址来源是 `ui`，`token_configured` 为 `true`，正文没有 `paddle-ui-token-demo`。

空密钥再次 PUT，文件里的密钥应保持不变：

```bash
curl -sS -X PUT http://127.0.0.1:8080/api/v1/intranet/ocr-config \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"expected_revision": 1, "intranet_paddle": {"token": ""}}'
```

清除界面密钥、改回环境变量：

```bash
curl -sS -X PUT http://127.0.0.1:8080/api/v1/intranet/ocr-config \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"expected_revision": 2, "intranet_paddle": {"clear_token": true}}'
```

MinerU 的 `api` / `tier` 同样放在 `intranet_mineru` 对象里，取值 `auto`、`v1`、`legacy`，档位沿用 wrapper 已支持的字符串（默认 `basic`）。

## 注入路径

不打印密钥，只核对子进程环境里哪些键被界面值替换：

```bash
python3 - << 'PY'
import os
from pathlib import Path
import sys
sys.path.insert(0, "ocr_provider")
os.environ["RETAIN_INTRANET_OCR_CONFIG_PATH"] = os.environ["RETAIN_INTRANET_OCR_CONFIG_PATH"]
from intranet_ocr_config import apply_persisted_intranet_env, format_apply_log

env = {
    "RETAIN_INTRANET_PADDLE_BASE_URL": "http://from-env:8080",
    "RETAIN_INTRANET_PADDLE_AUTH_SCHEME": "none",
    "RETAIN_INTRANET_PADDLE_TOKEN": "env-token-should-remain-if-ui-empty",
}
applied = apply_persisted_intranet_env(env, "intranet_paddle")
print(format_apply_log("intranet_paddle", applied))
print("base_url", env["RETAIN_INTRANET_PADDLE_BASE_URL"])
print("auth", env["RETAIN_INTRANET_PADDLE_AUTH_SCHEME"])
print("token_is_ui", env["RETAIN_INTRANET_PADDLE_TOKEN"] != "env-token-should-remain-if-ui-empty")
print("cloud_untouched", apply_persisted_intranet_env(dict(env), "paddle") == [])
PY
```

`local_command_driver.run_local_command_ocr_to_job_dir` 在 `subprocess.run` 之前调用 `_apply_intranet_ui_env`。因此下一次 `intranet_paddle` / `intranet_mineru` 任务会带上文件里的非空项；云端 provider 与 `local` 不会改这些变量。wrapper 仍只读 `RETAIN_INTRANET_*`。

文件权限应为 `0600`：

```bash
stat -c "%a %n" "$RETAIN_INTRANET_OCR_CONFIG_PATH"
```

## 自动测试

```bash
python3 -m pytest tests/test_intranet_ocr_config.py
node --experimental-strip-types --test tests/intranet-ocr-form.test.mjs
```
