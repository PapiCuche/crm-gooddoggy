import { Check, CircleHelp, Clock, TriangleAlert, Unplug, X } from "lucide-react";
import { useTranslations } from "next-intl";

import type { ChannelAccount } from "@/lib/api/model";
import {
  MessagingLimitTierEnum,
  PlatformEnum,
  QualityRatingEnum,
  StatusDetailEnum,
} from "@/lib/api/model";
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
  unknown: { Icon: CircleHelp, tone: "text-muted" }, // no se comprobó: ni bien ni mal (F7b-06h)
} as const;

// Los códigos que la API manda y que esta pantalla sabe traducir. Un código que no está (la API
// puede crecer antes que la pantalla, o traer una clave como `constructor`) nunca se busca en los
// mensajes: se dice con su texto de «desconocido», no con la clave en crudo.
const CODES = {
  detail: [StatusDetailEnum, "UNKNOWN"],
  quality: [QualityRatingEnum, "UNKNOWN"],
  tier: [MessagingLimitTierEnum, "TIER_NOT_SET"],
  platform: [PlatformEnum, "UNKNOWN"],
} as const;
export function useCodeLabel() {
  const t = useTranslations("channels");
  return (group: keyof typeof CODES, value: string | null | undefined): string => {
    const [known, fallback] = CODES[group];
    const code = value != null && Object.hasOwn(known, value) ? value : fallback;
    return t(`${group}.${code}` as never);
  };
}

// El estado de la cuenta, con su icono.
export function StatusMark({ status }: { status: ChannelAccount["status"] }) {
  const t = useTranslations("channels");
  // Un estado que esta versión no conoce (la API puede crecer antes que la pantalla) no la rompe:
  // se dice como desconocido, con el icono de «esperando». Solo claves propias: `Object.hasOwn`.
  const known = Object.hasOwn(STATUS, status);
  const { Icon, tone } = known ? STATUS[status] : STATUS.PENDING;
  return (
    <p className="flex items-center gap-1.5 text-sm font-medium">
      <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
      {t(known ? `status.${status}` : "status.UNKNOWN")}
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
      // `QUALITY_LOW` (la cuenta quedó `DEGRADED` porque Meta rechazó un envío por la calidad) avisa
      // aunque la última comprobación aún diga «buena»: no se contradice con el motivo de arriba.
      state:
        detail === "QUALITY_LOW" || quality === "YELLOW" || quality === "RED"
          ? "warn"
          : quality === "GREEN"
            ? "ok"
            : "wait",
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

// Lo que falta para usar una cuenta de Messenger o Instagram (F7b-06, ADR-035 §10): el token, la
// app del CRM suscrita a la página (F7b-06g), el primer evento firmado de Meta, en Instagram el
// acceso a los mensajes y qué app controla los hilos (`standby`). Hasta ver un evento no se sabe
// nada de los dos últimos: se espera. Una cuenta `PENDING` sin suscripción aún no lo vio. La
// suscripción solo sale bien si Meta lo dijo (`page_subscription`, F7b-06h); si no, «sin comprobar».
export function SocialChecklist({ account }: { account: ChannelAccount }) {
  const t = useTranslations("channels");
  const { status_detail: detail } = account;
  const unsubscribed = detail === "PAGE_NOT_SUBSCRIBED";
  const unseen = detail === "WEBHOOK_NOT_SEEN" || (unsubscribed && account.status === "PENDING");
  const steps: { key: string; state: keyof typeof STATE }[] = [
    {
      key: "token",
      state: account.token_last_four !== null && detail !== "TOKEN_INVALID" ? "ok" : "bad",
    },
    {
      key: "subscription",
      state: unsubscribed ? "bad" : account.page_subscription === "SUBSCRIBED" ? "ok" : "unknown",
    },
    { key: "webhook", state: unseen ? "wait" : "ok" },
    { key: "control", state: account.has_standby_conversations ? "warn" : unseen ? "wait" : "ok" },
  ];
  if (account.platform === "INSTAGRAM") {
    const access = detail === "MESSAGES_ACCESS_OFF" ? "bad" : unseen ? "wait" : "ok";
    steps.splice(3, 0, { key: "access", state: access });
  }
  return (
    <ul aria-label={t("checklist.label")} className="flex flex-col gap-1 text-sm">
      {steps.map(({ key, state }) => {
        const { Icon, tone } = STATE[state];
        return (
          <li key={key} className="flex items-center gap-2">
            <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
            <span>{t(`checklist.${key}.${state}` as never)}</span>
          </li>
        );
      })}
    </ul>
  );
}

// El aviso de `standby` (ADR-035 §4): otra app de la página controla al menos un hilo abierto. Sus
// mensajes llegan, pero aquí nunca se responde nada automáticamente en él.
export function Standby() {
  const t = useTranslations("channels.standby");
  return (
    <div role="note" className="border-danger flex gap-2 rounded-lg border p-3 text-sm">
      <TriangleAlert aria-hidden className="text-danger mt-0.5 size-4 shrink-0" />
      <p className="flex flex-col gap-0.5">
        <span className="font-medium">{t("title")}</span>
        <span>{t("body")}</span>
      </p>
    </div>
  );
}
