"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { usePriceListPricesCreate } from "@/lib/api/client";
import type { ListPrice, Price } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { localValue, stamp, ZONE, zonedInstant } from "./zoned";

// Lo que acepta la API (`core.money.AMOUNT`): hasta 12 cifras enteras y 2 decimales, con punto.
const AMOUNT = /^\d{1,12}(\.\d{1,2})?$/;
export const validAmount = (text: string) => AMOUNT.test(text) && /[1-9]/.test(text);
// En céntimos enteros, sin pasar por un `float`: «4399.5» son 439950.
export const cents = (text: string) => {
  const [whole, part = ""] = text.split(".");
  return Number(whole) * 100 + Number(part.padEnd(2, "0"));
};
type Text = "amount" | "reason";
// Qué le falta a un «Desde» programado: vacío, ilegible (un año de cinco cifras da `NaN`) o no
// futuro. `null`: vale.
export function startProblem(at: string, zone: string, now: Date) {
  if (!at) return "atRequired";
  const instant = zonedInstant(at, zone).getTime();
  if (Number.isNaN(instant)) return "atInvalid";
  return instant <= now.getTime() ? "atPast" : null;
}

// «Cambiar precio» de una variante en una lista (F3-45), con `POST …/price-lists/{id}/prices/`
// (F3-44): importe, «Desde» (ahora o una fecha y hora futura en la zona de la organización) y
// el motivo. Antes de confirmar enseña la diferencia con el vigente. El importe viaja como lo
// escribió el usuario, en string: nunca un número. Quién puede hacerlo lo decide la API.
export function PriceForm(props: {
  slug: string;
  listId: string;
  listKey: QueryKey;
  row: ListPrice;
  onAsk: () => void; // se abre: los avisos anteriores ya no aplican
  onDone: (text: string) => void;
  onStale: (here: boolean) => void; // 404: la lista o la variante ya no están
}) {
  const { slug, listId, listKey, row } = props;
  const t = useTranslations("prices.form");
  const format = useFormatter();
  const zone = useTimeZone() ?? ZONE;
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [later, setLater] = useState(false);
  const [at, setAt] = useState("");
  const [atError, setAtError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const currency = row.current?.currency ?? "PEN";
  const money = (value: number, sign = false) =>
    format.number(value, {
      ...{ style: "currency", currency },
      ...(sign && { signDisplay: "exceptZero" }),
    });
  const label = `${row.variant.product_name} ${row.variant.name} (${row.variant.sku})`;

  async function saved(price: Price) {
    await queryClient.invalidateQueries({ queryKey: listKey });
    setOpen(false);
    const amount = money(Number(price.amount));
    props.onDone(
      later
        ? t("scheduled", { sku: row.variant.sku, amount, at: stamp(price.starts_at, zone) })
        : t("done", { sku: row.variant.sku, amount }),
    );
  }
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const write = usePriceListPricesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: saved,
      onError: (error) => {
        // Un 409 dice que la fila cambió o ya rige ese precio: la tabla se vuelve a pedir.
        if (error.status === 409) void queryClient.invalidateQueries({ queryKey: listKey });
        if (error.status !== 404) return;
        const active = document.activeElement;
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        setOpen(false);
        props.onStale(here);
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve al botón que abrió, salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    write.reset();
    if (next) props.onAsk();
    setTyped("");
    setLater(false);
    setAt("");
    setAtError(null);
    setOpen(next);
  }
  // «Desde» en el futuro: lo comprueba el navegador para avisar antes, y la API otra vez.
  function check() {
    if (!later) return true;
    const problem = startProblem(at, zone, new Date());
    setAtError(problem && t(problem));
    return !problem;
  }

  // Se compara con lo que regirá en ese momento: programado después del próximo cambio, con él.
  const instant = later && at ? zonedInstant(at, zone).getTime() : NaN;
  const after = row.next && instant >= Date.parse(row.next.starts_at);
  const base = after ? row.next!.amount : row.current?.amount;
  let diff = null;
  if (validAmount(typed)) {
    const now = base ? cents(base) : 0;
    const change = cents(typed) - now;
    diff = !base
      ? t("first")
      : t(after ? "diffNext" : "diff", {
          amount: money(change / 100, true),
          percent: format.number(change / now, {
            ...{ style: "percent", maximumFractionDigits: 1 },
            signDisplay: "exceptZero",
          }),
        });
  }
  const choice = (value: boolean) => (
    <label className="flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
      <input
        type="radio"
        name={`${id}-when`}
        className="size-4 shrink-0"
        checked={later === value}
        aria-disabled={write.isPending}
        onChange={() => write.isPending || setLater(value)}
      />
      {t(value ? "later" : "now")}
    </label>
  );

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-3", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
      onInput={(event: FormEvent<HTMLDivElement>) => {
        const input = event.target as HTMLInputElement;
        if (input.name === "amount") setTyped(input.value.trim());
        // Solo lo que cambia «Desde» retira su aviso.
        if (atError && (input.type === "radio" || input.type === "datetime-local"))
          setAtError(null);
      }}
    >
      {open ? (
        <FieldsForm<Text>
          title={t("title", { name: label })}
          heading={<h2 className="font-medium wrap-anywhere">{t("title", { name: label })}</h2>}
          className="border-border flex w-full max-w-xl flex-col gap-4 border-t pt-4"
          fields={[
            {
              ...{ name: "amount", max: 15, label: t("amount"), inputMode: "decimal" },
              ...{ required: t("amountRequired"), invalid: t("amountInvalid") },
              hint: row.current
                ? t("amountHint", { amount: money(Number(row.current.amount)) })
                : t("amountHintFirst"),
              valid: validAmount,
            },
            {
              ...{ name: "reason", max: 500, label: t("reason"), hint: t("reasonHint") },
              ...{ required: t("reasonRequired"), invalid: t("reasonInvalid") },
              valid: (text) => text.length >= 3,
            },
          ]}
          note={
            <>
              <fieldset className="flex flex-col gap-1">
                <legend className="text-sm font-medium">{t("since")}</legend>
                {choice(false)}
                {choice(true)}
                {later ? (
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${id}-at`} className="text-sm font-medium">
                      {t("at")}
                    </label>
                    <input
                      id={`${id}-at`}
                      type="datetime-local"
                      value={at}
                      min={localValue(new Date(), zone)}
                      max="9999-12-31T23:59"
                      aria-invalid={atError || write.error?.fields?.starts_at ? true : undefined}
                      aria-describedby={`${id}-at-note`}
                      className="bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger h-11 rounded-[10px] border px-3 text-[16px]"
                      onChange={(event) => write.isPending || setAt(event.target.value)}
                    />
                    <p
                      id={`${id}-at-note`}
                      role={atError ? "alert" : undefined}
                      className={cn("text-sm", atError ? "text-danger" : "text-muted")}
                    >
                      {atError ?? t("atHint", { zone })}
                    </p>
                  </div>
                ) : null}
              </fieldset>
              {diff ? <p className="text-sm font-medium tabular-nums">{diff}</p> : null}
            </>
          }
          write={write}
          check={check}
          send={({ amount, reason }) => {
            const data = {
              ...{ variant_id: row.variant.id, amount, reason },
              ...(later && { starts_at: zonedInstant(at, zone).toISOString() }),
            };
            write.mutate({ orgSlug: slug, priceListId: listId, data });
          }}
          taken={(error) => {
            if (error.code === "PRICE_UNCHANGED") return { field: "amount", text: t("unchanged") };
            if (error.code === "PRICE_ALREADY_SCHEDULED") return { text: t("alreadyScheduled") };
            if (error.code === "PRICE_CONFLICT") return { text: t("conflict") };
            return error.fields?.starts_at ? { text: t("atPast") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t(later ? "schedule" : "save"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("openLabel", { name: label })}
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
