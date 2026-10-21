import { buildApiHeaders, buildApiUrl } from "@/platform/config/runtime.js";
import {
  INTRANET_OCR_CONFIG_PATH,
  createFetchIntranetOcrClient,
  type IntranetOcrClient,
} from "@/platform/api/domains/intranet-ocr-config.js";

/** Uses the same API base and X-API-Key headers as the rest of the RetainPDF web app. */
export function retainIntranetOcrClient(): IntranetOcrClient {
  return createFetchIntranetOcrClient({
    endpoint: buildApiUrl("", INTRANET_OCR_CONFIG_PATH.replace(/^\//, "")),
    headers: () => buildApiHeaders(),
  });
}
