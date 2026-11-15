"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { quotesDiscount } from "@/lib/api/client";
import type { QuoteDetail, QuoteDiscountTypeEnum } from "@/lib/api/model";

import { Field, FIELD as BASE, useFocusTrip, useQuoteProblem, useQuoteWrite } from "./quote-editor";

const FIGURE = /^\d{1,12}(\.\d{1,2})?$/; // como la API: sin signo ni exponente, hasta 2 decimales
const KINDS = ["PERCENT", "AMOUNT", "NONE"] as const;
const FIELD = `${BASE} h-11`;

// El descuento de una línea de un borrador (F5-09g): porcentaje, importe por unidad con IGV, o
// quitarlo. Se manda la `version` leída al abrir. Si pasa del límite de quien descuenta, la API
// deja la cotización «Por aprobar» y aquí se dice; el límite no se conoce antes: lo mide el
// servidor. Un 400 del valor (también el que dejaría el precio en cero) se explica en su campo.
export function LineDiscount({
  quote,
  line,
  onClose,
}: {
  quote: QuoteDetail;
  line: QuoteDetail["items"][number];
  onClose: (said?: string) => void;
}) {
  const t = useTranslations("quotes.discount");
  const slug = useTenant().organization.slug;
  const problemOf = useQuoteProblem();
  const id = useId();
  const form = useFocusTrip();
  const [version] = useState(quote.version ?? 1);
  const was = line.discount_type === "AMOUNT" ? "AMOUNT" : "PERCENT";
  const [kind, setKind] = useState<QuoteDiscountTypeEnum>(was);
  const [value, setValue] = useState(
    line.discount_type && line.discount_type !== "NONE" ? (line.discount_value ?? "") : "",
  );
  const [checked, setChecked] = useState(false);
  const apply = useQuoteWrite(
    quote.id,
    () =>
      quotesDiscount(slug, quote.id, line.id, {
        version,
        discount_type: kind,
        ...(kind === "NONE" ? {} : { discount_value: value }),
      }),
    (saved) => onClose(t(saved.status === "PENDING_APPROVAL" ? "pending" : "done")),
  );
  const busy = apply.isPending;
  const wrong = kind !== "NONE" && !FIGURE.test(value);
  const bad =
    apply.error?.code === "VALIDATION_ERROR" && !Object.hasOwn(apply.error.fields ?? {}, "version");
  const problem = checked && wrong ? t("invalid") : bad ? t("refused") : problemOf(apply.error);

  return (
    <form
      ref={form}
      noValidate
      aria-label={t("title", { product: line.product_name })}
      aria-busy={busy}
      className="flex flex-col gap-3 py-1"
      onSubmit={(event) => {
        event.preventDefault();
        setChecked(true);
        if (!wrong && !busy) apply.mutate();
      }}
    >
      <div className="flex flex-wrap items-end gap-3">
        <Field id={`${id}-kind`} label={t("kind")}>
          <select
            id={`${id}-kind`}
            value={kind}
            disabled={busy}
            className={FIELD}
            onChange={(event) => setKind(event.target.value as QuoteDiscountTypeEnum)}
          >
            {KINDS.map((option) => (
              <option key={option} value={option}>
                {t(`kinds.${option}`, { currency: line.currency })}
              </option>
            ))}
          </select>
        </Field>
        {kind !== "NONE" ? (
          <Field id={`${id}-value`} label={t(`kinds.${kind}`, { currency: line.currency })}>
            <input
              id={`${id}-value`}
              inputMode="decimal"
              value={value}
              readOnly={busy}
              aria-invalid={(checked && wrong) || bad ? true : undefined}
              className={`${FIELD} w-36`}
              onChange={(event) => setValue(event.target.value.trim())}
            />
          </Field>
        ) : null}
        <Button type="submit" variant="accent" className="min-h-11 sm:min-h-9" aria-disabled={busy}>
          {t("apply")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={() => busy || onClose()}
        >
          {t("cancel")}
        </Button>
      </div>
      <p className="text-muted text-sm">{t("limitHint")}</p>
      <p role="alert" className={problem ? "text-danger text-sm" : "sr-only"}>
        {problem}
      </p>
    </form>
  );
}
