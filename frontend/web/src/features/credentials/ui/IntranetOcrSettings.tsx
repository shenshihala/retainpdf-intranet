import { useCallback, useEffect, useState } from "react";
import {
  AUTH_SCHEME_OPTIONS,
  MINERU_API_OPTIONS,
  buildIntranetProviderUpdate,
  intranetSourceLabel,
  type IntranetOcrClient,
  type IntranetOcrConfigView,
  type IntranetProviderId,
  type IntranetProviderView,
} from "@/platform/api/domains/intranet-ocr-config.js";
import { SecretInput } from "./SecretInput.js";
import "./IntranetOcrSettings.css";

const SAVE_BUTTON_ID = "browser-credentials-save-btn";

function fieldId(providerId: string, name: string) {
  return `intranet-${providerId}-${name}`;
}

export function IntranetOcrSettings({
  providerId,
  client,
}: {
  providerId: IntranetProviderId;
  client: IntranetOcrClient;
}) {
  const [view, setView] = useState<IntranetOcrConfigView | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [authScheme, setAuthScheme] = useState("bearer");
  const [token, setToken] = useState("");
  const [apiMode, setApiMode] = useState("auto");
  const [tier, setTier] = useState("");
  const [clearToken, setClearToken] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [tone, setTone] = useState<"" | "valid" | "error">("");

  const applyView = useCallback((next: IntranetOcrConfigView, active: IntranetProviderId) => {
    const provider = next.providers?.[active];
    setView(next);
    if (!provider) return;
    setBaseUrl(provider.base_url || "");
    setAuthScheme(provider.auth_scheme || "bearer");
    setApiMode(provider.api || "auto");
    setTier(provider.tier || "");
    setToken("");
    setClearToken(false);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setMessage("");
    setTone("");
    client.load()
      .then((next) => {
        if (cancelled) return;
        applyView(next, providerId);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setMessage(error instanceof Error ? error.message : "无法读取内网 OCR 配置");
        setTone("error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applyView, client, providerId]);

  const persist = useCallback(async () => {
    const providerView: IntranetProviderView | undefined = view?.providers?.[providerId];
    if (!providerView || saving) return;
    setSaving(true);
    setMessage("正在保存…");
    setTone("");
    try {
      const next = await client.save({
        expected_revision: view?.revision,
        [providerId]: buildIntranetProviderUpdate({
          providerId,
          view: providerView,
          baseUrl,
          authScheme,
          token,
          clearToken,
          api: apiMode,
          tier,
        }),
      });
      applyView(next, providerId);
      setMessage("已保存。下一次识别任务会使用这些设置，无需重启容器。");
      setTone("valid");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
      setTone("error");
    } finally {
      setSaving(false);
    }
  }, [apiMode, applyView, authScheme, baseUrl, clearToken, client, providerId, saving, tier, token, view]);

  useEffect(() => {
    const button = document.getElementById(SAVE_BUTTON_ID);
    if (!button) return;
    const onClick = () => {
      void persist();
    };
    button.addEventListener("click", onClick);
    return () => button.removeEventListener("click", onClick);
  }, [persist]);

  const providerView = view?.providers?.[providerId];
  const showMinerU = providerId === "intranet_mineru";
  const tokenHint = providerView?.token_configured
    ? `已配置 ${providerView.token_masked || "••••"}（${intranetSourceLabel(providerView.token_source)}）。留空则保持不变。`
    : "未配置密钥。认证方式为「不使用密钥」时可以留空。";

  return (
    <div className="intranet-ocr-settings" data-intranet-ocr-settings={providerId}>
      <p className="credential-ocr-hint">
        保存在服务器的数据目录中。服务地址或密钥留空时，继续使用容器环境变量里的默认值。
      </p>
      {loading ? <p className="intranet-ocr-status">正在读取内网 OCR 配置…</p> : null}
      <label className="intranet-ocr-field">
        <span className="developer-label">服务地址</span>
        <input
          id={fieldId(providerId, "base-url")}
          value={baseUrl}
          autoComplete="off"
          spellCheck={false}
          placeholder="http://ocr.example.local:8080"
          disabled={loading || saving}
          onChange={(event) => setBaseUrl(event.target.value)}
        />
        {providerView ? (
          <p className="intranet-ocr-source">来源：{intranetSourceLabel(providerView.base_url_source)}</p>
        ) : null}
      </label>
      <label className="intranet-ocr-field">
        <span className="developer-label">认证方式</span>
        <select
          id={fieldId(providerId, "auth-scheme")}
          value={authScheme}
          disabled={loading || saving}
          onChange={(event) => setAuthScheme(event.target.value)}
        >
          {AUTH_SCHEME_OPTIONS.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
        {providerView ? (
          <p className="intranet-ocr-source">来源：{intranetSourceLabel(providerView.auth_scheme_source)}</p>
        ) : null}
      </label>
      <label className="intranet-ocr-field">
        <span className="developer-label">密钥</span>
        <SecretInput
          id={fieldId(providerId, "token")}
          secretLabel="密钥"
          autoComplete="off"
          placeholder="留空则保持已保存的密钥"
          value={token}
          disabled={loading || saving || clearToken}
          onChange={(event) => setToken(event.target.value)}
        />
        <p className="intranet-ocr-source">{tokenHint}</p>
      </label>
      {providerView?.token_source === "ui" ? (
        <label className="intranet-ocr-clear">
          <input
            id={fieldId(providerId, "clear-token")}
            type="checkbox"
            checked={clearToken}
            disabled={loading || saving}
            onChange={(event) => {
              setClearToken(event.target.checked);
              if (event.target.checked) setToken("");
            }}
          />
          <span>清除已保存的密钥，改回环境变量</span>
        </label>
      ) : null}
      {showMinerU ? (
        <>
          <label className="intranet-ocr-field">
            <span className="developer-label">API 模式</span>
            <select
              id={fieldId(providerId, "api")}
              value={apiMode}
              disabled={loading || saving}
              onChange={(event) => setApiMode(event.target.value)}
            >
              {MINERU_API_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
            {providerView ? (
              <p className="intranet-ocr-source">来源：{intranetSourceLabel(providerView.api_source || "empty")}</p>
            ) : null}
          </label>
          <label className="intranet-ocr-field">
            <span className="developer-label">档位</span>
            <input
              id={fieldId(providerId, "tier")}
              value={tier}
              autoComplete="off"
              spellCheck={false}
              placeholder="basic"
              disabled={loading || saving}
              onChange={(event) => setTier(event.target.value)}
            />
            {providerView ? (
              <p className="intranet-ocr-source">
                来源：{intranetSourceLabel(providerView.tier_source || "empty")}。仅 v1 请求会带上 tier。
              </p>
            ) : null}
          </label>
        </>
      ) : null}
      <div className="intranet-ocr-actions">
        <button
          id={fieldId(providerId, "save")}
          type="button"
          className="app-button secondary"
          disabled={loading || saving || !providerView}
          onClick={() => {
            void persist();
          }}
        >
          {saving ? "正在保存…" : "保存内网设置"}
        </button>
        <button
          type="button"
          className="app-button secondary"
          disabled={loading || saving}
          onClick={() => {
            setLoading(true);
            client.load()
              .then((next) => {
                applyView(next, providerId);
                setMessage("");
                setTone("");
              })
              .catch((error: unknown) => {
                setMessage(error instanceof Error ? error.message : "无法读取内网 OCR 配置");
                setTone("error");
              })
              .finally(() => setLoading(false));
          }}
        >
          重新读取
        </button>
      </div>
      <p
        id={fieldId(providerId, "status")}
        className={`intranet-ocr-status${tone ? ` is-${tone}` : ""}`}
        role="status"
        aria-live="polite"
      >
        {message}
      </p>
    </div>
  );
}
