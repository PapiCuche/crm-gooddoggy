"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import type { ListMargin } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

import type { useMargins } from "./use-margins";

// Fuera de este rango el porcentaje no dice nada (un precio de céntimos da billones): se recorta.
const CAP = 999;

type Costs = ReturnType<typeof useMargins>;

// El interruptor «Ver costos y margen» y lo que pasa al pedirlos. Un `<input role="switch">`
// nativo (teclado y lector de pantalla gratis) con una pista que solo anima `transform` y color,
// y no se anima con «reducir movimiento». Se pulsa a menudo: 150 ms, sin rebote.
export function MarginSwitch({ costs }: { costs: Costs }) {
  const t = useTranslations("prices.margins");
  const errors = useTranslations("errors");
  const { active, declined, query } = costs;
  const noteId = useId();
  const status = query.error?.status;
  const stepUp = query.error?.code === "STEP_UP_REQUIRED";
  let note: { text: string; alert: boolean } | null = null;
  if (declined) note = { text: t("declined"), alert: false };
  else if (active && query.isError && status !== 401) {
    let key = "";
    if (stepUp)
      key = "stepUp"; // un segundo STEP_UP_REQUIRED tras verificar no es falta de permiso
    else if (status === 403) key = "denied";
    else if (status === 404) key = "gone";
    note = { text: key ? t(key) : errors(`api.${apiErrorKey(query.error)}`), alert: true };
  } else if (active && query.isPending && query.fetchStatus !== "idle") {
    note = { text: t("loading"), alert: false };
  }
  return (
    <div className="flex flex-col gap-1 sm:col-span-2">
      <label className="flex min-h-11 w-fit cursor-pointer items-center gap-3 text-sm font-medium sm:min-h-9">
        <input
          type="checkbox"
          role="switch"
          data-margins-switch
          className="peer sr-only"
          checked={active}
          aria-busy={active && query.isFetching}
          aria-describedby={noteId}
          onChange={(event) => costs.toggle(event.target.checked)}
        />
        <span
          aria-hidden
          className="bg-border peer-checked:bg-foreground peer-focus-visible:outline-foreground relative h-6 w-11 shrink-0 rounded-full transition-colors duration-150 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 motion-reduce:transition-none peer-checked:[&>span]:translate-x-5"
        >
          <span className="bg-surface absolute top-0.5 left-0.5 size-5 rounded-full transition-transform duration-150 ease-(--ease-out) motion-reduce:transition-none" />
        </span>
        {t("switch")}
      </label>
      <p id={noteId} className="text-muted text-sm">
        {t(active ? "igv" : "hint")}
      </p>
      <p
        role={note?.alert ? "alert" : "status"}
        className={cn("text-sm", note ? (note.alert ? "text-danger" : "text-muted") : "sr-only")}
      >
        {note?.text ?? ""}
      </p>
      {costs.dialog}
      {note?.alert && (stepUp || (status !== 403 && status !== 404)) ? (
        <Button
          variant="ghost"
          className="border-border min-h-11 w-fit border sm:min-h-9"
          onClick={() => void query.refetch()}
        >
          {errors("retry")}
        </Button>
      ) : null}
    </div>
  );
}

// Importe con moneda. `format.number` no lanza con una moneda inválida (la registra y devuelve el
// número solo), así que se comprueba antes: una moneda que Intl no conoce no tumba la fila.
export function moneyText(
  format: ReturnType<typeof useFormatter>,
  amount: string,
  currency: string,
) {
  try {
    new Intl.NumberFormat("es-PE", { style: "currency", currency });
  } catch {
    return `${amount} ${currency}`;
  }
  return format.number(Number(amount) || 0, { style: "currency", currency }); // `|| 0`: sin «-0»
}

// Costo, margen en importe y margen en % de una fila. Un margen negativo se marca con texto
// («Pérdida») y con color, no solo con color. Sin margen, `reason` dice por qué. Importes con IGV
// incluido, como los guarda la API.
export function MarginCells({ margin, waiting }: { margin?: ListMargin; waiting: boolean }) {
  const t = useTranslations("prices.margins");
  const format = useFormatter();
  const money = (amount: string, currency: string) => moneyText(format, amount, currency);
  const figure = margin?.margin;
  const side = margin?.price;
  const loss = !!figure && (Number(figure.amount) < 0 || Number(figure.percent) < 0);
  let percent = "";
  if (figure) {
    const value = Math.round(Number(figure.percent) * 10) / 10 || 0; // «-0.0» es 0
    if (value < -CAP) percent = t("capLow", { limit: CAP });
    else if (value > CAP) percent = t("capHigh", { limit: CAP });
    else {
      percent = format.number(value / 100, {
        ...{ style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1 },
      });
    }
  }
  const cell = (label: string, value: string, tone?: string, span?: boolean) => (
    <div className={cn("min-w-0", span && "basis-full sm:basis-auto")}>
      <dt className="text-muted text-[13px]">{label}</dt>
      <dd className={cn("wrap-anywhere", tone)}>{value}</dd>
    </div>
  );
  const dash = waiting ? "…" : "—";
  const tone = loss ? "text-danger font-medium" : undefined;
  return (
    <dl className="flex flex-wrap gap-x-6 gap-y-1 text-sm tabular-nums sm:justify-end">
      {cell(t("cost"), margin?.cost ? money(margin.cost.amount, margin.cost.currency) : dash)}
      {figure && side ? (
        <>
          {cell(
            t("marginAmount"),
            `${money(figure.amount, side.currency)}${loss ? ` · ${t("loss")}` : ""}`,
            tone,
          )}
          {cell(t("marginPercent"), percent, tone)}
        </>
      ) : (
        cell(
          t("marginAmount"),
          margin?.reason ? t(`reason.${margin.reason}`) : dash,
          undefined,
          true,
        )
      )}
    </dl>
  );
}
