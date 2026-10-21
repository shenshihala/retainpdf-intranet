# 内网 OCR 设置（overlay）

> 开源包说明：本目录不包含真实密钥。请先复制示例环境文件再启动：
>
> ```bash
> cp docker/app.env.example docker/app.env
> cp docker/web.env.example docker/web.env
> cp docker/auth.local.json.example docker/auth.local.json
> # 编辑上述文件：填入你的内网 OCR 地址与密钥；勿提交到 Git
> ```
>
> 本包未附带构建好的 `web-dist/`。请用 `../sources/frontend/` 中的补丁重建前端，或按 [WEB_OVERLAY.md](WEB_OVERLAY.md) 同步到 RetainPDF 源码后 `npm run build`，再把产物放到本目录的 `web-dist/` 与 `web-html/`。

这是挂在开源 RetainPDF 上的内网 Docker overlay。用户可以在接口设置里修改内网 PaddleOCR-VL 和内网 MinerU 的服务地址、密钥、认证方式，以及 MinerU 的 API 模式和档位。保存后写入数据卷，下一次 OCR 任务生效，不必为了改地址或密钥执行 `docker compose up --force-recreate`。

`docker/app.env` 里的 `RETAIN_INTRANET_*` 仍是回退值：界面没填的项继续用环境变量。云端 Paddle / MinerU 的提交方式不变。

## 本地查看设置界面

配置接口和预览页是两套进程。预览只用于核对表单，正式页面仍是 RetainPDF 设置里的 OCR 卡片。

```bash
mkdir -p /tmp/intranet-ocr-demo/secrets
RETAIN_INTRANET_OCR_CONFIG_BIND=127.0.0.1 \
RETAIN_INTRANET_OCR_CONFIG_PORT=8080 \
RETAIN_INTRANET_OCR_CONFIG_PATH=/tmp/intranet-ocr-demo/secrets/intranet-ocr-config.json \
RETAIN_INTRANET_OCR_CONFIG_API_KEYS=YOUR_PROXY_API_KEY \
RETAIN_INTRANET_PADDLE_BASE_URL=http://host.docker.internal:8080 \
RETAIN_INTRANET_PADDLE_AUTH_SCHEME=none \
RETAIN_INTRANET_MINERU_BASE_URL=http://host.docker.internal:8081 \
RETAIN_INTRANET_MINERU_AUTH_SCHEME=bearer \
RETAIN_INTRANET_MINERU_API=v1 \
RETAIN_INTRANET_MINERU_TIER=basic \
python3 ocr_provider/intranet_ocr_config_server.py
```

另开一个终端：

```bash
cd preview && npm install && npm run dev
```

打开 [内网 OCR 设置](http://127.0.0.1:8080)。接口直接访问时用 `http://127.0.0.1:8080`。

## 测试

```bash
python3 -m pytest tests/test_intranet_ocr_config.py
node --experimental-strip-types --test tests/intranet-ocr-form.test.mjs
```

核对清单见 [VERIFICATION.md](VERIFICATION.md)。接到现有 `retainpdf-intranet` 目录的步骤见 [WEB_OVERLAY.md](WEB_OVERLAY.md)。

## 布局

| 路径 | 作用 |
| --- | --- |
| `ocr_provider/intranet_ocr_config.py` | 读写 `/data/secrets/intranet-ocr-config.json`，并把非空项注入 `RETAIN_INTRANET_*` |
| `ocr_provider/intranet_ocr_config_server.py` | `GET`/`PUT /api/v1/intranet/ocr-config` |
| `ocr_provider/local_command_driver.py` | 启动子进程前调用上面的注入 |
| `ocr_provider/intranet_*_wrapper.py` | 仍只读环境变量，不打开配置文件 |
| `frontend/web/src/...` | 拷进 retain-pdf 同源路径后重新构建 web-dist |
| `docker/nginx.conf.template` | 把该 API 转到 sidecar，其余 `/api/` 仍进 Rust |