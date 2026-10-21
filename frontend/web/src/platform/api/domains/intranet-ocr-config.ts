/** Client for the intranet OCR settings sidecar (GET/PUT /api/v1/intranet/ocr-config). */

export type IntranetProviderId = "intranet_paddle" | "intranet_mineru";
export type IntranetValueSource = "ui" | "env" | "default" | "empty";

export interface IntranetProviderView {
  base_url: string;
  base_url_source: IntranetValueSource;
  auth_scheme: string;
  auth_scheme_source: IntranetValueSource;
  token_configured: boolean;
  token_source: "ui" | "env" | "empty";
  token_masked: string;
  api?: string;
  api_source?: IntranetValueSource;
  tier?: string;
  tier_source?: IntranetValueSource;
}

export interface IntranetOcrConfigView {
  schema: string;
  revision: number;
  providers: Record<IntranetProviderId, IntranetProviderView>;
}

export interface IntranetProviderUpdate {
  base_url?: string;
  auth_scheme?: string;
  token?: string;
  clear_token?: boolean;
  api?: string;
  tier?: string;
}

export interface IntranetOcrConfigUpdate {
  expected_revision?: number;
  intranet_paddle?: IntranetProviderUpdate;
  intranet_mineru?: IntranetProviderUpdate;
}

export interface IntranetOcrClient {
  load: () => Promise<IntranetOcrConfigView>;
  save: (body: IntranetOcrConfigUpdate) => Promise<IntranetOcrConfigView>;
}

export const INTRANET_OCR_CONFIG_PATH = "/api/v1/intranet/ocr-config";

export const AUTH_SCHEME_OPTIONS = [
  { value: "bearer", label: "Bearer" },
  { value: "token", label: "Token" },
  { value: "raw", label: "原始 Authorization" },
  { value: "none", label: "不使用密钥" },
] as const;

export const MINERU_API_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "v1", label: "v1（/v1/file_parse）" },
  { value: "legacy", label: "旧版（/file_parse）" },
] as const;

/**
 * Non-secret fields: an unchanged env/default value is omitted (sent as "")
 * so saving does not pin app.env into the UI file. An existing UI override is
 * written back as edited, including an empty string that clears it.
 */
export function nonSecretOverride(edited: string, viewValue: string, source: string): string {
  const next = `${edited || ""}`.trim();
  if (source === "ui") return next;
  if (next === `${viewValue || ""}`.trim()) return "";
  return next;
}

export function buildIntranetProviderUpdate(input: {
  providerId: IntranetProviderId;
  view: IntranetProviderView;
  baseUrl: string;
  authScheme: string;
  token: string;
  clearToken: boolean;
  api?: string;
  tier?: string;
}): IntranetProviderUpdate {
  const update: IntranetProviderUpdate = {
    base_url: nonSecretOverride(input.baseUrl, input.view.base_url, input.view.base_url_source),
    auth_scheme: nonSecretOverride(
      input.authScheme,
      input.view.auth_scheme,
      input.view.auth_scheme_source,
    ),
  };
  const token = `${input.token || ""}`.trim();
  if (input.clearToken) {
    update.clear_token = true;
  } else if (token) {
    update.token = token;
  }
  if (input.providerId === "intranet_mineru") {
    update.api = nonSecretOverride(input.api || "", input.view.api || "", input.view.api_source || "empty");
    update.tier = nonSecretOverride(
      input.tier || "",
      input.view.tier || "",
      input.view.tier_source || "empty",
    );
  }
  return update;
}

export function intranetSourceLabel(source: string): string {
  if (source === "ui") return "界面配置";
  if (source === "env") return "环境变量";
  if (source === "default") return "默认值";
  return "未设置";
}

interface ErrorEnvelope {
  message?: string;
  code?: string;
}

export function createFetchIntranetOcrClient(options: {
  endpoint: string;
  headers?: () => Record<string, string>;
}): IntranetOcrClient {
  async function request(method: "GET" | "PUT", body?: IntranetOcrConfigUpdate) {
    const headers = {
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers?.() || {}),
    };
    let response: Response;
    try {
      response = await fetch(options.endpoint, {
        method,
        headers,
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store",
      });
    } catch {
      throw new Error("无法连接内网 OCR 配置接口");
    }
    const text = await response.text();
    let parsed: { data?: IntranetOcrConfigView; message?: string } = {};
    if (text) {
      try {
        parsed = JSON.parse(text) as { data?: IntranetOcrConfigView; message?: string };
      } catch {
        parsed = {};
      }
    }
    if (!response.ok) {
      const message = `${(parsed as ErrorEnvelope).message || ""}`.trim()
        || `保存失败（${response.status}）`;
      throw new Error(message);
    }
    if (!parsed.data || typeof parsed.data !== "object") {
      throw new Error("内网 OCR 配置响应无效");
    }
    return parsed.data;
  }

  return {
    load: () => request("GET"),
    save: (body) => request("PUT", body),
  };
}
