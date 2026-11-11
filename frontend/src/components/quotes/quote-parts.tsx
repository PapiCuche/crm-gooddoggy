import { CalendarX, Check, Clock, Eye, Pencil, Send, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { moneyText } from "@/components/pricing/margins";
import type { QuoteStatusEnum } from "@/lib/api/model";
import { cn } from "@/lib/utils";

// Cada estado de una cotización (F5-09f) con icono y texto, nunca solo color. Un estado que esta
// versión no conoce no rompe la pantalla (solo claves propias: `Object.hasOwn`): se dice como
// desconocido, no con su valor en crudo.
const STATUS = {
  DRAFT: { Icon: Pencil, tone: "text-muted" },
  PENDING_APPROVAL: { Icon: Clock, tone: "text-muted" },
  APPROVED: { Icon: Check, tone: "text-success" },
  SENT: { Icon: Send, tone: "text-foreground" },
  VIEWED: { Icon: Eye, tone: "text-foreground" },
  ACCEPTED: { Icon: Check, tone: "text-success" },
  REJECTED: { Icon: X, tone: "text-danger" },
  EXPIRED: { Icon: CalendarX, tone: "text-danger" },
  CANCELLED: { Icon: X, tone: "text-muted" },
} as const;

export function QuoteStatus({ status }: { status: QuoteStatusEnum }) {
  const t = useTranslations("quotes.statuses");
  const known = Object.hasOwn(STATUS, status);
  const { Icon, tone } = known ? STATUS[status] : { Icon: Clock, tone: "text-muted" };
  return (
    <span className="flex items-center gap-1.5 text-sm font-medium">
      <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
      {t(known ? status : "UNKNOWN")}
    </span>
  );
}

// Una enviada o vista con la validez pasada: la API no la pasa a `EXPIRED` por sí sola (ese cambio
// llega con el ciclo de vida), pero sí la cuenta en el filtro `expired`: la pantalla la marca igual.
export const isLate = (quote: { status: string; valid_until?: string | null }, now: number) =>
  (quote.status === "SENT" || quote.status === "VIEWED") &&
  !!quote.valid_until &&
  Date.parse(quote.valid_until) < now;

// Un importe de la API (texto con dos decimales) con su moneda. Solo se formatea: nunca se suma ni
// se multiplica en el navegador. Sin importe, un guion.
export function useMoney() {
  const format = useFormatter();
  const dash = useTranslations("quotes.detail")("none");
  return (amount: string | undefined, currency: string) =>
    amount === undefined ? dash : moneyText(format, amount, currency);
}

// La tasa de la API es una fracción en texto («0.1800»): se dice en por ciento moviendo la coma
// sobre el texto, sin pasar por números de coma flotante.
export function ratePercent(rate: string): string {
  const digits = (rate.split(".")[1] ?? "").padEnd(4, "0").slice(0, 4);
  const tail = digits.slice(2).replace(/0+$/, "");
  return `${Number(digits.slice(0, 2))}${tail ? `,${tail}` : ""}`;
}

// Fecha corta de un instante de la API, en la zona de la pantalla.
export function useDay() {
  const format = useFormatter();
  return (at: string) =>
    format.dateTime(new Date(at), { day: "numeric", month: "short", year: "numeric" });
}
