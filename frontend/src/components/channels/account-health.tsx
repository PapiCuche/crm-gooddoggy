import { Check, Clock, TriangleAlert, Unplug, X } from "lucide-react";
import { useTranslations } from "next-intl";

import type { ChannelAccount } from "@/lib/api/model";
import { cn } from "@/lib/utils";

// La salud de una cuenta de WhatsApp (F7-07d), con lo que calcula F7-06b: cada estado y cada paso
// lleva icono y texto, nunca solo color. Todo sale de códigos de la API que aquí se traducen.
const STATUS = {
  PENDING: { Icon: Clock, tone: "text-muted" },
  CONNECTED: { Icon: Check, tone: "text-success" },
  DEGRADED: { Icon: TriangleAlert, tone: "text-danger" },
  DISCONNECTED: { Icon: Unplug, tone: "text-muted" },
  ERROR: { Icon: X, tone: "text-danger" },
} as const;
const STATE = {
  ok: { Icon: Check, tone: "text-success" },
  warn: { Icon: TriangleAlert, tone: "text-danger" },
  bad: { Icon: X, tone: "text-danger" },
  wait: { Icon: Clock, tone: "text-muted" },
} as const;

// El estado de la cuenta, con su icono.
export function StatusMark({ status }: { status: ChannelAccount["status"] }) {
  const t = useTranslations("channels");
  const { Icon, tone } = STATUS[status];
  return (
    <p className="flex items-center gap-1.5 text-sm font-medium">
      <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
      {t(`status.${status}`)}
    </p>
  );
}

// Lo que falta para usar la cuenta. El token vale si hay credencial y no está invalidada. El
// primer mensaje firmado de Meta está visto salvo que `status_detail` diga lo contrario: una
// cuenta `PENDING` sin detalle ya lo vio y espera su primera comprobación de salud. La calidad
// espera esa comprobación (`UNKNOWN`), avisa si es media o baja y vale si es buena.
export function Checklist({ account }: { account: ChannelAccount }) {
  const t = useTranslations("channels");
  const { status_detail: detail, quality_rating: quality } = account;
  const steps = [
    {
      key: "token",
      state: account.token_last_four !== null && detail !== "TOKEN_INVALID" ? "ok" : "bad",
    },
    { key: "webhook", state: detail === "WEBHOOK_NOT_SEEN" ? "wait" : "ok" },
    {
      key: "quality",
      state:
        quality === "GREEN" ? "ok" : quality === "YELLOW" || quality === "RED" ? "warn" : "wait",
    },
  ] as const;
  return (
    <ul aria-label={t("checklist.label")} className="flex flex-col gap-1 text-sm">
      {steps.map(({ key, state }) => {
        const { Icon, tone } = STATE[state];
        return (
          <li key={key} className="flex items-center gap-2">
            <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
            <span>{t(`checklist.${key}.${state}`)}</span>
          </li>
        );
      })}
    </ul>
  );
}
