import { useMemo } from "react";
import {
  credentialTokenInputId,
  credentialValidateButtonId,
  credentialValidationId,
} from "./credentials-dom-ids.js";
import { useCredentialsController } from "./useCredentialsController.js";
import { OCR_PROVIDER_DEFINITIONS, ocrProviderRequiresToken } from "@/platform/config/providers.js";
import { validationIcon } from "../domain/validation-icon.js";
import { ExternalLink, PlugZap } from "lucide-react";
import { SecretInput } from "./SecretInput.js";
import {
  resetHandlerFor,
} from "./provider-panel-shared.js";
import { IntranetOcrSettings } from "./IntranetOcrSettings.js";
import { retainIntranetOcrClient } from "./retain-intranet-ocr-client.js";
import type { IntranetProviderId } from "@/platform/api/domains/intranet-ocr-config.js";

export function OcrPanels() {
  const { credentials, view, handlers, tokenInputRef } = useCredentialsController();
  const activeProvider = credentials.ocrProvider;
  const intranetClient = useMemo(() => retainIntranetOcrClient(), []);

  return (
    <div className="credential-provider-panels">
      {OCR_PROVIDER_DEFINITIONS.map((provider) => {
        const active = provider.id === activeProvider;
        const validation = view.validations[provider.id] || { message: "", tone: "" };
        const content = `${validation.message || ""}`.trim();
        const badgeClasses = [
          "token-inline-status",
          content ? "" : "hidden",
          validation.tone === "valid" ? "is-valid" : "",
          validation.tone === "error" ? "is-error" : "",
          content && validation.tone === "pending" ? "is-pending" : "",
        ].filter(Boolean).join(" ");
        const needsToken = ocrProviderRequiresToken(provider.id);
        const hint = `${provider.validationUnavailableMessage || provider.description || ""}`.trim();

        return (
          <section
            key={provider.id}
            className={`credential-panel credential-provider-panel${active ? " is-active" : ""}`}
            data-ocr-provider-panel={provider.id}
            hidden={!active}
          >
            {provider.intranetSettings ? (
              active ? (
                <IntranetOcrSettings
                  providerId={provider.id as IntranetProviderId}
                  client={intranetClient}
                />
              ) : null
            ) : needsToken ? (
              <label>
                <span className="developer-label">{provider.tokenLabel}</span>
                <SecretInput
                  id={credentialTokenInputId(provider.id)}
                  secretLabel={provider.tokenLabel}
                  autoComplete="off"
                  placeholder={provider.tokenPlaceholder}
                  defaultValue=""
                  ref={tokenInputRef(provider.id)}
                  onInput={() => resetHandlerFor(handlers)?.()}
                />
              </label>
            ) : hint ? (
              <p className="credential-ocr-hint">{hint}</p>
            ) : null}
            {needsToken && !provider.supportsValidation && hint ? (
              <p className="credential-ocr-hint">{hint}</p>
            ) : null}
            <div className="credential-card-footer">
              <div className="credential-card-actions">
                {provider.supportsValidation ? (
                  <button
                    id={credentialValidateButtonId(provider.id)}
                    type="button"
                    className="app-button secondary"
                    disabled={validation.tone === "pending"}
                    onClick={() => handlers?.validateOcr?.()}
                  >
                    <PlugZap aria-hidden="true" />
                    {provider.validationButtonLabel}
                  </button>
                ) : null}
                <span
                  id={credentialValidationId(provider.id)}
                  className={badgeClasses}
                  title={content || provider.validationIdleMessage}
                  aria-label={content || provider.validationIdleMessage}
                  role="status"
                  aria-live="polite"
                >
                  {validationIcon(validation.tone, content)}
                </span>
                {provider.docsUrl ? (
                  <a className="credential-card-link" href={provider.docsUrl} target="_blank" rel="noopener noreferrer">
                    {provider.docsLabel}
                    <ExternalLink aria-hidden="true" />
                  </a>
                ) : null}
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}
