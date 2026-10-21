import type {
  CredentialsFields,
  OcrTokenOptions,
} from "./state-types.js";
import { normalizeOcrProvider, ocrProviderRequiresToken } from "@/platform/config/providers.js";

export function ocrTokenFromCredentials(
  credentials: Partial<CredentialsFields> = {},
  { defaultPaddleToken, providerId }: OcrTokenOptions = {},
): string {
  if (normalizeOcrProvider(providerId || credentials.ocrProvider) === "mineru") {
    return credentials.mineruToken || "";
  }
  const token = credentials.paddleToken;
  if (token) {
    return token;
  }
  return defaultPaddleToken?.() || "";
}

export function hasCompleteCredentials(
  credentials: Partial<CredentialsFields> = {},
  options: OcrTokenOptions = {},
): boolean {
  const providerId = normalizeOcrProvider(options.providerId || credentials.ocrProvider);
  const ocrReady = !ocrProviderRequiresToken(providerId)
    || Boolean(credentials.ocrCredentialRef || ocrTokenFromCredentials(credentials, options));
  return Boolean(
    ocrReady
    && (credentials.modelApiKey || credentials.translationCredentialRef),
  );
}
