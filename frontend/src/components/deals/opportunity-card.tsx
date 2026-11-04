import { useFormatter, useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { moneyText } from "@/components/pricing/margins";
import type { Opportunity } from "@/lib/api/model";

const DAY = 86_400_000;

// Días completos que lleva en su etapa. `now`, de la pantalla: una sola hora para todas las tarjetas.
export const daysInStage = (opportunity: Opportunity, now: number) =>
  Math.max(0, Math.floor((now - Date.parse(opportunity.stage_entered_at)) / DAY));

// Importes como céntimos enteros: sumar decimales en coma flotante deja restos (0,1 + 0,2).
export const cents = (amount: string) => Math.round(Number(amount) * 100) || 0;

// Una oportunidad en el tablero: título y número, cliente, importe, responsable, días en la etapa
// y próxima acción. `actions` es el hueco de las acciones de la tarjeta (mover, ganar, perder):
// la pantalla las ofrece según lo que la API permita; la tarjeta solo presenta.
export function OpportunityCard({
  opportunity,
  owner,
  now,
  actions,
}: {
  opportunity: Opportunity;
  owner: string;
  now: number;
  actions?: ReactNode;
}) {
  const t = useTranslations("deals.card");
  const format = useFormatter();
  const { contact, next_action: next } = opportunity;
  return (
    <li
      id={`opportunity-${opportunity.id}`}
      tabIndex={-1} // destino del foco al crear una oportunidad (F5-05c)
      className="border-border bg-background flex flex-col gap-2 rounded-[10px] border p-3 leading-snug"
    >
      <p className="flex flex-col">
        <span className="font-medium wrap-anywhere">{opportunity.title}</span>
        <span className="text-muted font-mono text-[13px]">{opportunity.number}</span>
      </p>
      {opportunity.status === "OPEN" ? null : (
        <p className="text-sm font-medium wrap-anywhere">
          {opportunity.status === "WON"
            ? t("won")
            : opportunity.lost_reason
              ? t("lostBecause", { reason: opportunity.lost_reason.name })
              : t("lost")}
        </p>
      )}
      <p className="text-sm wrap-anywhere">{contact.display_name ?? t("noContact")}</p>
      <p className="font-medium">{moneyText(format, opportunity.amount, opportunity.currency)}</p>
      <p className="text-muted flex flex-col text-sm">
        <span className="wrap-anywhere">{owner}</span>
        <span>{t("days", { days: daysInStage(opportunity, now) })}</span>
        <span className="wrap-anywhere">{next ? t("next", { text: next }) : t("noNext")}</span>
      </p>
      {actions}
    </li>
  );
}
