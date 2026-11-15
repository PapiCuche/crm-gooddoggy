"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTimeZone, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { type Target, TargetPicker } from "@/components/pricing/target-picker";
import { localValue, ZONE, zonedInstant } from "@/components/pricing/zoned";
import { Button } from "@/components/ui/button";
import {
  getQuotesListQueryKey,
  getQuotesRetrieveQueryKey,
  quotesCreate,
  quotesUpdate,
} from "@/lib/api/client";
import type { QuoteDetail } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

type Line = { key: number; variant: Target | null; quantity: string };
export const FIELD =
  "bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger rounded-[10px] border px-3 text-[16px]";
// Lo que un 400 dice por campo, con su texto; uno sin texto propio es el general.
const FIELDS = ["items", "variant_id", "valid_until", "reason", "comment", "_"] as const;
const CODES = [
  ...["STALE_VERSION", "QUOTE_NOT_DRAFT", "QUOTE_REFUSED", "NOT_PENDING"],
  ...["SELF_APPROVAL_FORBIDDEN", "BEYOND_LIMIT", "PRICE_NOT_ALLOWED"],
];
const MAX_LINES = 50; // como la API
// Lo que admite el campo de fecha: fuera de ahí no se convierte (un año de 3 cifras rompe la zona).
const DAYS = { min: "2000-01-01", max: "2999-12-31" };

// El texto de un error de escritura de cotizaciones, por su `code` (nunca el de la respuesta).
// Un 401 no se explica aquí: la sesión lo atiende.
export function useQuoteProblem() {
  const t = useTranslations("quotes.errors");
  const api = useTranslations("errors.api");
  return (error: ApiError | null): string | null => {
    if (!error || error.status === 401) return null;
    if (error.code === "VALIDATION_ERROR") {
      const field = FIELDS.find((name) => Object.hasOwn(error.fields ?? {}, name));
      return t(`fields.${field ?? "other"}`);
    }
    return CODES.includes(error.code) ? t(error.code) : api(apiErrorKey(error));
  };
}

// Una escritura sobre una cotización: su respuesta pasa a ser el detalle y las listas se vuelven a
// pedir. Un 404 o un 409 vuelve a leer el detalle: lo que se mostraba ya no vale.
export function useQuoteWrite(
  quoteId: string | undefined,
  call: () => Promise<QuoteDetail>,
  done: (saved: QuoteDetail) => void,
) {
  const slug = useTenant().organization.slug;
  const client = useQueryClient();
  return useMutation<QuoteDetail, ApiError, void>({
    networkMode: "always",
    mutationFn: call,
    onError: async (error) => {
      if (quoteId && (error.status === 404 || error.status === 409)) {
        await client.invalidateQueries({ queryKey: getQuotesRetrieveQueryKey(slug, quoteId) });
      }
    },
    onSuccess: async (saved) => {
      client.setQueryData(getQuotesRetrieveQueryKey(slug, saved.id), saved);
      await client.invalidateQueries({ queryKey: getQuotesListQueryKey(slug) });
      done(saved);
    },
  });
}

export function Field(props: {
  id: string;
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={`flex flex-col gap-1.5 ${props.className ?? ""}`}>
      <label htmlFor={props.id} className="text-sm font-medium">
        {props.label}
      </label>
      {props.children}
    </div>
  );
}

// Si el control que tenía el foco ya no está, el foco va al título del detalle (no se pierde).
export const focusHome = (from: Element | null = document.activeElement) => {
  if (!from || from === document.body || !from.isConnected)
    document.querySelector<HTMLElement>("[data-focus-home]")?.focus();
};

// Al abrir un formulario, el foco pasa a su primer control; al cerrarlo, vuelve a donde estaba al
// abrirlo, leído al dibujarlo: antes de que el formulario al que sustituye devuelva el suyo. Si ese
// botón ya no está (la acción ya no se ofrece), va al título.
export function useFocusTrip() {
  const form = useRef<HTMLFormElement>(null);
  const [back] = useState(() =>
    typeof document === "undefined" ? null : (document.activeElement as HTMLElement | null),
  );
  useEffect(() => {
    form.current?.querySelector<HTMLElement>("input, select, textarea")?.focus();
    return () => (back?.isConnected ? back.focus() : focusHome(back));
  }, [back]);
  return form;
}

const label = (line: QuoteDetail["items"][number]) =>
  `${line.product_name} · ${line.variant_name} (${line.sku})`;

// Crear un borrador desde un cliente o una oportunidad (`link`), o cambiar uno (`quote`) (F5-09g):
// variantes con su cantidad, la validez y las notas. El precio no se envía: lo pone el motor de
// precios al guardar, con la lista de la cotización. Al cambiar, solo viaja lo cambiado, con la
// `version` leída al abrir (409 si otra persona cambió algo). Lo decide todo la API.
export function QuoteEditor({
  link,
  quote: shown,
  onCancel,
  onSaved,
}: {
  link?: { contact_id: string } | { opportunity_id: string };
  quote?: QuoteDetail;
  onCancel: () => void;
  onSaved: (saved: QuoteDetail) => void;
}) {
  const t = useTranslations("quotes.editor");
  const zone = useTimeZone() ?? ZONE;
  const slug = useTenant().organization.slug;
  const problemOf = useQuoteProblem();
  const id = useId();
  const form = useFocusTrip();
  // La cotización como estaba al abrir: si se vuelve a leer mientras se escribe, lo que se compara
  // y la `version` que se envía siguen siendo los que vio quien edita.
  const [quote] = useState(shown);
  const [lines, setLines] = useState<Line[]>(() =>
    quote
      ? quote.items.map((x, key) => ({
          key,
          variant: { id: x.variant_id, label: label(x) },
          quantity: String(x.quantity),
        }))
      : [{ key: 0, variant: null, quantity: "1" }],
  );
  const opened = quote?.valid_until
    ? localValue(new Date(quote.valid_until), zone).slice(0, 10)
    : "";
  const [day, setDay] = useState(opened);
  const [customer, setCustomer] = useState(quote?.notes_to_customer ?? "");
  const [internal, setInternal] = useState(quote?.internal_notes ?? "");
  const [checked, setChecked] = useState(false);
  const save = useQuoteWrite(
    quote?.id,
    () => {
      const items = lines.map((x) => ({ variant_id: x.variant!.id, quantity: Number(x.quantity) }));
      const until = day ? zonedInstant(`${day}T23:59`, zone).toISOString() : null;
      if (!quote) {
        return quotesCreate(slug, {
          ...link,
          items,
          ...(until ? { valid_until: until } : {}),
          notes_to_customer: customer,
          internal_notes: internal,
        });
      }
      const before = quote.items.map((x) => `${x.variant_id}:${x.quantity}`).join();
      const changed = {
        ...(items.map((x) => `${x.variant_id}:${x.quantity}`).join() !== before ? { items } : {}),
        ...(day !== opened ? { valid_until: until } : {}),
        ...(customer !== (quote.notes_to_customer ?? "") ? { notes_to_customer: customer } : {}),
        ...(internal !== (quote.internal_notes ?? "") ? { internal_notes: internal } : {}),
      };
      return quotesUpdate(slug, quote.id, { version: quote.version ?? 1, ...changed });
    },
    onSaved,
  );
  const busy = save.isPending;
  const whole = (text: string) => /^\d{1,7}$/.test(text) && +text >= 1 && +text <= 1_000_000;
  const incomplete = lines.some((x) => !x.variant || !whole(x.quantity));
  const late = day !== "" && !(day >= DAYS.min && day <= DAYS.max);
  const problem =
    checked && incomplete ? t("incomplete") : checked && late ? t("badDay") : problemOf(save.error);
  const taken = new Set(lines.flatMap((x) => (x.variant ? [x.variant.id] : [])));
  const change = (key: number, patch: Partial<Line>) =>
    setLines((all) => all.map((x) => (x.key === key ? { ...x, ...patch } : x)));
  const text = (name: "customer" | "internal", value: string, set: (v: string) => void) => (
    <Field id={`${id}-${name}`} label={t(name)}>
      <textarea
        id={`${id}-${name}`}
        rows={3}
        value={value}
        maxLength={2000}
        readOnly={busy}
        className={`${FIELD} min-h-20 resize-y py-2.5`}
        onChange={(event) => set(event.target.value)}
      />
    </Field>
  );

  return (
    <form
      ref={form}
      noValidate
      aria-label={t(quote ? "editTitle" : "newTitle")}
      aria-busy={busy}
      className="border-border bg-surface flex max-w-3xl flex-col gap-5 rounded-lg border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        setChecked(true);
        if (!incomplete && !late && !busy) save.mutate();
      }}
    >
      <h2 className="text-lg font-semibold">{t(quote ? "editTitle" : "newTitle")}</h2>
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-1 font-medium">{t("lines")}</legend>
        <p className="text-muted text-sm">{t(quote ? "repriceHint" : "priceHint")}</p>
        {lines.map((x, index) => (
          <div
            key={x.key}
            className="border-border flex flex-col gap-3 rounded-lg border p-3 sm:flex-row sm:items-start"
          >
            <div className="min-w-0 flex-1">
              <TargetPicker
                slug={slug}
                kind="variant_id"
                label={t("variant", { n: index + 1 })}
                picked={x.variant}
                taken={taken}
                onPick={(variant) => change(x.key, { variant })}
              />
            </div>
            <Field id={`${id}-q${x.key}`} label={t("quantity")} className="sm:w-28">
              <input
                id={`${id}-q${x.key}`}
                inputMode="numeric"
                value={x.quantity}
                readOnly={busy}
                aria-invalid={checked && !whole(x.quantity) ? true : undefined}
                className={`${FIELD} h-11`}
                onChange={(event) => change(x.key, { quantity: event.target.value.trim() })}
              />
            </Field>
            {lines.length > 1 ? (
              <Button
                type="button"
                variant="ghost"
                className="border-border min-h-11 border sm:mt-7 sm:min-h-9"
                aria-label={t("removeLabel", { n: index + 1 })}
                onClick={() => setLines((all) => all.filter((other) => other.key !== x.key))}
              >
                {t("remove")}
              </Button>
            ) : null}
          </div>
        ))}
        <Button
          type="button"
          variant="ghost"
          className="border-border min-h-11 self-start border sm:min-h-9"
          aria-disabled={lines.length >= MAX_LINES}
          aria-describedby={lines.length >= MAX_LINES ? `${id}-cap` : undefined}
          onClick={() =>
            lines.length < MAX_LINES &&
            setLines((all) => [
              ...all,
              { key: Math.max(-1, ...all.map((x) => x.key)) + 1, variant: null, quantity: "1" },
            ])
          }
        >
          {t("add")}
        </Button>
        {lines.length >= MAX_LINES ? (
          <p id={`${id}-cap`} className="text-muted text-sm">
            {t("cap", { max: MAX_LINES })}
          </p>
        ) : null}
      </fieldset>
      <Field id={`${id}-until`} label={t("validUntil")}>
        <input
          id={`${id}-until`}
          type="date"
          {...DAYS}
          value={day}
          readOnly={busy}
          aria-invalid={checked && late ? true : undefined}
          aria-describedby={`${id}-until-hint`}
          className={`${FIELD} h-11 sm:w-56`}
          onChange={(event) => setDay(event.target.value)}
        />
        <p id={`${id}-until-hint`} className="text-muted text-sm">
          {t("validHint", { zone })}
        </p>
      </Field>
      {text("customer", customer, setCustomer)}
      {text("internal", internal, setInternal)}
      <p role="alert" className={problem ? "text-danger text-sm" : "sr-only"}>
        {problem}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="accent" className="min-h-11 sm:min-h-9" aria-disabled={busy}>
          {t(quote ? "save" : "create")}
        </Button>
        <Button
          type="button"
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={() => busy || onCancel()}
        >
          {t("cancel")}
        </Button>
      </div>
    </form>
  );
}
