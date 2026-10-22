import { Cpu } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { LLMSettings } from "@/lib/api";

interface Props {
  settings: LLMSettings | null;
  runtimeProvider?: string;
  runtimeModel?: string;
  runtimeReasoningEffort?: string;
}

export function ModelRuntimeBar({
  settings,
  runtimeProvider,
  runtimeModel,
}: Props) {
  const { t } = useTranslation();
  if (!settings) return null;

  // The bar is a view of the current configuration. Session metadata is
  // historical and must not mask a model change made in Settings.
  const providerId = settings.provider;
  const provider = settings.providers.find((item) => item.name === providerId);
  const providerLabel = provider?.label || providerId || t("agent.unknownProvider");
  const model = settings.model_name || t("agent.unknownModel");
  const runtimeProviderOption = settings.providers.find((item) => item.name === runtimeProvider);
  const runtimeProviderLabel = runtimeProviderOption?.label || runtimeProvider || t("agent.unknownProvider");
  const sessionModelDiffers = Boolean(
    (runtimeProvider && runtimeProvider !== settings.provider)
    || (runtimeModel && runtimeModel !== settings.model_name),
  );
  const effortLabels: Record<string, string> = {
    none: t("settings.reasoningEffortNone"),
    low: t("settings.reasoningEffortLow"),
    medium: t("settings.reasoningEffortMedium"),
    high: t("settings.reasoningEffortHigh"),
    max: t("settings.reasoningEffortMax"),
  };
  const reasoningEffort = settings.reasoning_effort;
  const effortLabel = effortLabels[reasoningEffort] || t("settings.providerDefault");

  return (
    <div className="shrink-0 border-b border-border/70 bg-background/95 px-6 py-2 backdrop-blur-sm">
      <div className="mx-auto flex max-w-3xl items-center gap-2 overflow-hidden text-xs">
        <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
          <span className="absolute inline-flex h-full w-full rounded-full bg-success/30" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
        </span>
        <span className="shrink-0 font-medium text-foreground">{providerLabel}</span>
        <span className="text-muted-foreground/60">·</span>
        <span className="truncate font-mono text-[11px] text-muted-foreground" title={model}>{model}</span>
        {sessionModelDiffers && (
          <span
            className="hidden truncate text-[10px] text-muted-foreground/70 xl:inline"
            title={`${runtimeProviderLabel} · ${runtimeModel || t("agent.unknownModel")}`}
          >
            {t("agent.previousSessionModel", {
              provider: runtimeProviderLabel,
              model: runtimeModel || t("agent.unknownModel"),
            })}
          </span>
        )}
        <span className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border/70 bg-muted/35 px-2 py-0.5 text-[10px] text-muted-foreground">
          <Cpu className="h-3 w-3" aria-hidden="true" />
          {t("agent.reasoningStrength")}: {effortLabel}
        </span>
      </div>
    </div>
  );
}
