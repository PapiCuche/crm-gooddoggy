import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { moneyText } from "@/components/pricing/margins";
import type { Opportunity } from "@/lib/api/model";

import { daysInStage } from "./opportunity-card";

// Una oportunidad en la pestaña de la ficha (F5-06), con todo lo que trae la lista ; el cliente no se repite. `id` y `tabIndex`: destino del foco al crear, como en el tablero.
export function CustomerOpportunity({
  opportunity,
  owner,
  now,
}: {
  opportunity: Opportunity;
  owner: string;
  now: number;
}) {
  const t = useTranslations("deals");
  const format = useFormatter();
  const slug = useTenant().organization.slug;
  const { status, lost_reason: reason, next_action: next } = opportunity;
  let state = t("tab.open");
  if (status === "WON") state = t("card.won");
  if (status === "LOST")
    state = reason ? t("card.lostBecause", { reason: reason.name }) : t("card.lost");
  return (
    <li
      id={`opportunity-${opportunity.id}`}
      tabIndex={-1}
      className="border-border bg-surface flex flex-col gap-2 rounded-lg border p-4 leading-snug focus-visible:outline-none"
    >
      <p className="flex flex-col">
        <Link
          href={`/o/${slug}/oportunidades/${opportunity.id}`}
          prefetch={false}
          className="font-medium wrap-anywhere underline-offset-4 hover:underline"
        >
          {opportunity.title}
        </Link>
        <span className="text-muted font-mono text-[13px]">{opportunity.number}</span>
      </p>
      <p className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
        <span className="font-medium wrap-anywhere">{state}</span>
        <span className="wrap-anywhere">{t("tab.stage", { stage: opportunity.stage.name })}</span>
      </p>
      <p className="font-medium">{moneyText(format, opportunity.amount, opportunity.currency)}</p>
      <p className="text-muted flex flex-col text-sm">
        <span className="wrap-anywhere">{owner}</span>
        <span>{t("card.days", { days: daysInStage(opportunity, now) })}</span>
        <span className="wrap-anywhere">
          {next ? t("card.next", { text: next }) : t("card.noNext")}
        </span>
      </p>
    </li>
  );
}
