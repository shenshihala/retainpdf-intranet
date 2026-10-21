# 部署到现有 retainpdf-intranet overlay

在现有 overlay 目录里合入本包，然后**只为挂载新文件重建一次** web 与 sidecar。之后在界面改服务地址或密钥，不要再 `docker compose up --force-recreate`。

从 `docker/app.env.example` 复制得到的 `docker/app.env` 里，`RETAIN_INTRANET_*` 请保留为界面留空时的回退。不要用示例里的 `RETAIN_INTRANET_MINERU_TOKEN=YOUR_MINERU_TOKEN` 覆盖线上真实密钥；从 `docker/app.env.example` 复制后填入你自己的值，或改到界面里填写。

## 1. 挂载

`docker-compose.yml` 需要这三处增量：

1. `app` 增加只读挂载  
   `./ocr_provider/intranet_ocr_config.py` →  
   `/usr/local/lib/python3.11/site-packages/retainpdf_pipeline/ocr/ocr_provider/intranet_ocr_config.py`
2. 新服务 `intranet-ocr-config`（见本包 `docker-compose.yml`）。镜像 `python:3.11-slim-bookworm`，用户 `10001:10001`，与 app 共用 `app_data:/data`，并挂载 `docker/auth.local.json`。
3. `web` 增加  
   `./docker/nginx.conf.template:/etc/nginx/templates/default.conf.template:ro`  
   并 `depends_on` sidecar 健康。官方 web 镜像启动时会对这个模板做 envsubst，`${RETAINPDF_PROXY_API_KEY}` 仍由 `docker/web.env` 注入。

同时替换这三个已有挂载（行为兼容，只是多了启动前注入）：

- `ocr_provider/local_command_driver.py`
- `ocr_provider/intranet_paddle_layout_wrapper.py`
- `ocr_provider/intranet_mineru_wrapper.py`

配置文件实际路径是 `/data/secrets/intranet-ocr-config.json`（权限 `0600`，目录 `0700`）。可用 `RETAIN_INTRANET_OCR_CONFIG_PATH` 改路径。文件不存在时，任务继续使用 `app.env`。

## 2. 重新构建前端

把本包这些文件拷进 retain-pdf 工作树的同名路径（不要改云端 paddle / mineru 的 token 字段）：

```text
frontend/web/src/platform/config/providers.ts
frontend/web/src/platform/api/domains/intranet-ocr-config.ts
frontend/web/src/features/credentials/ui/OcrPanels.tsx
frontend/web/src/features/credentials/ui/IntranetOcrSettings.tsx
frontend/web/src/features/credentials/ui/IntranetOcrSettings.css
frontend/web/src/features/credentials/ui/retain-intranet-ocr-client.ts
```

`SecretInput.tsx` 使用上游原文件，不必覆盖。`payload.ts` 保持现状：只有云端 `paddle` 发送 `paddle_api_url` / `paddle_token`。内网 provider 的 `requiresToken` 仍是 `false`。

```bash
cd /path/to/retain-pdf/frontend/web && npm run build:js
rsync -a --delete dist/ /path/to/retainpdf-intranet-overlay/web-dist/
cp index.html admin.html detail.html reader.html /path/to/retainpdf-intranet-overlay/web-html/
```

必须整目录替换 `web-dist`，不要只换 `app.bundle.js`。esbuild 的 hashed chunk 和 HTML 是一起的。

## 3. 启动

```bash
docker compose up -d
```

这一次会重建 web（nginx 模板）并拉起 sidecar。确认：

```bash
docker compose ps
curl -fsS http://127.0.0.1:30000/health
```

浏览器打开站点的接口设置，选择「内网 PaddleOCR-VL」或「内网 MinerU」，填写后点「保存内网设置」。点原有的「保存接口」时，若当前选中的是内网 provider，也会一并提交。

## 4. 生效方式

| 改了什么 | 要不要重建容器 |
| --- | --- |
| 服务地址、密钥、认证方式、MinerU API / 档位 | 不要。下一次 OCR 任务由 `local_command_driver` 读文件并写入子进程环境 |
| 第一次加入 sidecar、nginx 模板、Python 挂载、web-dist | 要。这是挂载本身的变更 |
| `app.env` 里的回退值 | 要重建 app 与 sidecar 后，环境变量才会变。界面里已保存的非空项仍优先于环境变量 |

密钥不会出现在接口日志里。`GET` 只返回是否已配置和掩码（末四位，短密钥则只有 `••••`）。`PUT` 里密钥为空表示保留原值；勾选「清除已保存的密钥」或传 `clear_token: true` 才删掉界面保存的密钥，并回到环境变量。

## 5. 兼容性

app 镜像 v4.2.6 对翻译请求使用 `deny_unknown_fields`。本 overlay 不改翻译 payload，也不给云端 OCR 增加字段。内网 provider 在 `ocr_providers.json` 里仍是 `local_command`，命令仍是原来的 wrapper 模块。
