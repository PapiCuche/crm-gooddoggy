import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { moneyText } from "@/components/pricing/margins";
import type { Opportunity } from "@/lib/api/model";

import { daysInStage } from "./opportunity-card";

const COLUMNS = [
  "number",
  "title",
  "customer",
  "stage",
  "amount",
  "owner",
  "days",
  "next",
] as const;

// La misma lista como tabla: una fila por oportunidad, con la etapa en una columna. En 390 px
// la tabla se desplaza dentro de su región (que recibe el foco) y la página no.
export function OpportunityTable({
  rows,
  owner,
  now,
}: {
  rows: Opportunity[];
  owner: (id: string | null) => string;
  now: number;
}) {
  const t = useTranslations("deals");
  const format = useFormatter();
  const slug = useTenant().organization.slug;
  return (
    <div role="region" aria-label={t("table.label")} tabIndex={0} className="overflow-x-auto">
      <table className="bg-surface border-border w-full min-w-[56rem] border text-left text-sm">
        <thead className="border-border border-b">
          <tr>
            {COLUMNS.map((key) => (
              <th key={key} scope="col" className="px-3 py-2 font-medium">
                {t(`table.${key}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-border border-b align-top last:border-b-0">
              <td className="px-3 py-2 font-mono text-[13px]">{row.number}</td>
              <td className="px-3 py-2 font-medium wrap-anywhere">
                <Link
                  href={`/o/${slug}/oportunidades/${row.id}`}
                  prefetch={false}
                  className="underline-offset-4 hover:underline"
                >
                  {row.title}
                </Link>
              </td>
              <td className="px-3 py-2 wrap-anywhere">
                {row.contact.display_name ?? t("card.noContact")}
              </td>
              <td className="px-3 py-2">{row.stage.name}</td>
              <td className="px-3 py-2 whitespace-nowrap">
                {moneyText(format, row.amount, row.currency)}
              </td>
              <td className="px-3 py-2 wrap-anywhere">{owner(row.assigned_user_id)}</td>
              <td className="px-3 py-2">{daysInStage(row, now)}</td>
              <td className="px-3 py-2 wrap-anywhere">{row.next_action ?? t("card.noNext")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
