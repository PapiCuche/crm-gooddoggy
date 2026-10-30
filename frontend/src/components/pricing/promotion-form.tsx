"use client";

import { useTimeZone, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { usePromotionsCreate } from "@/lib/api/client";
import type { DiscountTypeEnum, PriceList, Promotion } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { validAmount } from "./price-form";
import { KINDS } from "./promotion-targets";
import { type Kind, type Target, TargetPicker } from "./target-picker";
import { ZONE, zonedInstant } from "./zoned";

const TYPES = ["PERCENT_OFF", "AMOUNT_OFF", "FIXED_PRICE"] as const;
const [ITEMS_MAX, LISTS_MAX] = [100, 20]; // los límites de la API
type Row = { key: number; kind: Kind; target: Target | null; own: string };
type Problems = Record<string, true>; // por clave: `name`, `row.0`, `own.1`…
type Form = Record<"name" | "description" | "terms" | "value" | "starts" | "ends", string> & {
  type: DiscountTypeEnum;
  lists: string[] | null; // `null`: sin tocar, rige la predeterminada
  rows: Row[];
};
const FRESH: Form = {
  ...{ name: "", description: "", terms: "", value: "", starts: "", ends: "" },
  ...{ type: "PERCENT_OFF", lists: null, rows: [] },
};
const SERVER_KEY: Record<string, string> = {
  ...{ name: "name", description: "description", terms: "terms", discount_type: "type" },
  ...{ value: "value", currency: "lists", price_list_ids: "lists" },
  ...{ starts_at: "starts", ends_at: "ends" },
};
const DATE = { type: "datetime-local", max: "9999-12-31T23:59" };
const isRecord = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

// Los errores 400 de la API por campo (`fields`). Los de un objetivo llegan anidados por su
// posición (`items["0"].variant_id`): el de `override_value` es de su descuento propio.
function serverProblems(fields: Record<string, unknown> = {}): Problems {
  const out: Problems = {};
  for (const [key, value] of Object.entries(fields)) {
    if (key !== "items") {
      if (SERVER_KEY[key]) out[SERVER_KEY[key]] = true;
      continue;
    }
    const rows = Array.isArray(value) ? Object.fromEntries(value.entries()) : value;
    const nested = isRecord(rows) && Object.values(rows).every(isRecord);
    if (!nested) out.items = true;
    else {
      for (const [index, row] of Object.entries(rows)) {
        const bad = row as Record<string, unknown>;
        if (Object.keys(bad).length) out[`${bad.override_value ? "own" : "row"}.${index}`] = true;
      }
    }
  }
  return out;
}

// «Nueva promoción» (F3-48g) en un `<dialog>` modal, con `POST …/pricing/promotions/` (F3-48e):
// nace en borrador. La moneda es la de las listas elegidas. Se valida en el navegador solo lo
// evidente; la API decide y su 400 sale junto al campo (o a la fila) que nombra.
export function PromotionForm(props: {
  slug: string;
  priceLists: PriceList[]; // las activas
  onCreated: (promotion: Promotion) => void;
  onClose: () => void;
}) {
  const { slug, priceLists } = props;
  const t = useTranslations("promotions.form");
  const errors = useTranslations("errors.api");
  const zone = useTimeZone() ?? ZONE;
  const dialog = useRef<HTMLDialogElement>(null);
  const sending = useRef(false); // un envío por pulsación: la mutación tarda una tarea en decirlo
  const next = useRef(1);
  const id = useId();
  const [form, setForm] = useState<Form>(() => ({
    ...FRESH,
    rows: [{ key: 0, kind: "category_id", target: null, own: "" }],
  }));
  const [bad, setBad] = useState<Problems>({});
  const write = usePromotionsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: props.onCreated,
      onError: () => (sending.current = false),
    },
  });
  const busy = write.isPending;
  const failure = write.error as ApiError | null;
  const served = failure?.code === "VALIDATION_ERROR" ? serverProblems(failure.fields) : {};
  const problems: Problems = { ...served, ...bad };
  const flagged = Object.keys(problems).join();
  const generic = failure && !Object.keys(served).length && failure.status !== 401;

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    dialog.current?.querySelector<HTMLElement>("input")?.focus();
  }, []);
  useEffect(() => {
    if (flagged)
      dialog.current?.querySelector<HTMLElement>("[aria-invalid],[data-problem]")?.focus();
  }, [flagged, failure]);

  const chosen = form.lists ?? priceLists.filter((list) => list.is_default).map((l) => l.id);
  const picked = priceLists.filter((list) => chosen.includes(list.id));
  const currency = picked[0]?.currency ?? "PEN";
  const unit = (type = form.type) =>
    type === "PERCENT_OFF" ? "%" : currency === "PEN" ? "S/" : currency;
  function patch(change: Partial<Form>) {
    if (sending.current) return;
    setBad({});
    if (write.isError) write.reset();
    setForm((now) => ({ ...now, ...change }));
  }
  const row = (key: number, change: Partial<Row>) =>
    patch({ rows: form.rows.map((one) => (one.key === key ? { ...one, ...change } : one)) });

  function submit() {
    if (sending.current) return;
    const found: Problems = {};
    const at = (local: string) => zonedInstant(local, zone);
    const [from, to] = [at(form.starts), at(form.ends)];
    if (!form.name.trim()) found.name = true;
    if (!validAmount(form.value)) found.value = true;
    if (!form.starts || Number.isNaN(from.getTime())) found.starts = true;
    if (!form.ends || Number.isNaN(to.getTime()) || to <= from) found.ends = true;
    if (!chosen.length || new Set(picked.map((l) => l.currency)).size > 1) found.lists = true;
    form.rows.forEach((one, index) => {
      if (!one.target) found[`row.${index}`] = true;
      if (one.own && !validAmount(one.own)) found[`own.${index}`] = true;
    });
    setBad(found);
    if (Object.keys(found).length) return;
    sending.current = true;
    write.mutate({
      orgSlug: slug,
      data: {
        ...{ name: form.name.trim(), discount_type: form.type, value: form.value, currency },
        ...{ starts_at: from.toISOString(), ends_at: to.toISOString() },
        price_list_ids: chosen,
        ...(form.description.trim() && { description: form.description.trim() }),
        ...(form.terms.trim() && { terms: form.terms.trim() }),
        items: form.rows.map((one) => ({
          [one.kind]: one.target!.id,
          ...(one.own && { override_value: one.own }),
        })),
      },
    });
  }

  const say = (key: string, text: string) =>
    problems[key] ? t(`err.${text}` as "err.name") : undefined;
  const group = (key: string, body: ReactNode) => (
    <>
      {body}
      {problems[key] ? (
        <p data-problem tabIndex={-1} role="alert" className="text-danger text-sm">
          {t(`err.${key.split(".")[0]}` as "err.name")}
        </p>
      ) : null}
    </>
  );
  const area = (name: "description" | "terms") =>
    group(
      name,
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${id}-${name}`} className="text-sm font-medium">
          {t(name)}
        </label>
        <textarea
          id={`${id}-${name}`}
          rows={3}
          maxLength={2000}
          value={form[name]}
          aria-invalid={problems[name] ? true : undefined}
          className="bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger rounded-[10px] border px-3 py-2 text-[16px]"
          onChange={(event) => patch({ [name]: event.target.value })}
        />
      </div>,
    );
  const text = (name: "name" | "value" | "starts" | "ends", more = {}) => (
    <TextField
      label={name === "value" ? t(`valueLabel.${form.type}`, { unit: unit() }) : t(name)}
      value={form[name]}
      maxLength={name === "name" ? 200 : 15}
      error={say(name, name)}
      onChange={(event) => patch({ [name]: event.target.value })}
      {...more}
    />
  );
  const button = "min-h-11 sm:min-h-9";

  return (
    <dialog
      ref={dialog}
      aria-labelledby={`${id}-title`}
      className="bg-surface text-foreground border-border fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-none w-full max-w-none overflow-y-auto border-l p-0 backdrop:bg-black/40 sm:max-w-xl"
      onClose={props.onClose}
      onCancel={(event) => sending.current && event.preventDefault()}
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <form
        noValidate
        aria-busy={busy}
        className="flex min-h-full flex-col gap-4 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <h2 id={`${id}-title`} className="text-lg font-semibold">
          {t("title")}
        </h2>
        {text("name")}
        {area("description")}
        {area("terms")}
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">{t("type")}</legend>
          {TYPES.map((type) => (
            <label key={type} className={`flex items-center gap-2 text-sm ${button}`}>
              <input
                type="radio"
                name={`${id}-type`}
                className="size-4 shrink-0"
                checked={form.type === type}
                onChange={() => patch({ type })}
              />
              {t(`types.${type}`, { unit: unit(type) })}
            </label>
          ))}
        </fieldset>
        {text("value", {
          inputMode: "decimal",
          autoComplete: "off",
          hint: t(`valueHint.${form.type}`),
        })}
        {text("starts", { ...DATE, hint: t("zoneHint", { zone }) })}
        {text("ends", DATE)}
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">{t("lists")}</legend>
          {group(
            "lists",
            priceLists.length ? (
              priceLists.map((list) => (
                <label key={list.id} className={`flex items-center gap-2 text-sm ${button}`}>
                  <input
                    type="checkbox"
                    className="size-4 shrink-0"
                    checked={chosen.includes(list.id)}
                    onChange={() =>
                      patch({
                        lists: chosen.includes(list.id)
                          ? chosen.filter((one) => one !== list.id)
                          : [...chosen, list.id].slice(0, LISTS_MAX),
                      })
                    }
                  />
                  {list.name} ({list.currency})
                </label>
              ))
            ) : (
              <p className="text-muted text-sm">{t("noLists")}</p>
            ),
          )}
        </fieldset>
        <fieldset className="flex flex-col gap-3">
          <legend className="mb-1 text-sm font-medium">{t("targets")}</legend>
          {group(
            "items",
            form.rows.map((one, index) => (
              <div
                key={one.key}
                className="border-border flex flex-col gap-3 rounded-lg border p-3"
              >
                <Choice
                  label={t("kind", { n: index + 1 })}
                  value={one.kind}
                  busy={busy}
                  onChange={(kind) => row(one.key, { kind: kind as Kind, target: null })}
                  options={KINDS.map((kind): [string, string] => [kind, t(`kinds.${kind}`)])}
                />
                <TargetPicker
                  slug={slug}
                  kind={one.kind}
                  label={t(`kinds.${one.kind}`)}
                  picked={one.target}
                  error={say(`row.${index}`, "row")}
                  onPick={(target) => row(one.key, { target })}
                />
                <TextField
                  label={t("own", { unit: unit() })}
                  value={one.own}
                  inputMode="decimal"
                  maxLength={15}
                  hint={t("ownHint")}
                  error={say(`own.${index}`, "own")}
                  onChange={(event) => row(one.key, { own: event.target.value })}
                />
                <Button
                  type="button"
                  variant="ghost"
                  className={`${button} border-border self-start border`}
                  aria-disabled={form.rows.length === 1}
                  aria-label={t("remove", { n: index + 1 })}
                  onClick={() =>
                    form.rows.length > 1 &&
                    patch({ rows: form.rows.filter((r) => r.key !== one.key) })
                  }
                >
                  {t("removeTarget")}
                </Button>
              </div>
            )),
          )}
          <Button
            type="button"
            variant="ghost"
            className={`${button} border-border self-start border`}
            aria-disabled={form.rows.length >= ITEMS_MAX}
            onClick={() =>
              form.rows.length < ITEMS_MAX &&
              patch({
                rows: [
                  ...form.rows,
                  { key: next.current++, kind: "category_id", target: null, own: "" },
                ],
              })
            }
          >
            {t("addTarget")}
          </Button>
        </fieldset>
        {generic ? (
          <p role="alert" className="text-danger text-sm">
            {failure.code === "PERMISSION_DENIED"
              ? t("denied")
              : errors(
                  failure.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(failure),
                )}
          </p>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="ghost"
            className={button}
            aria-disabled={busy}
            onClick={() => sending.current || dialog.current?.close()}
          >
            {t("cancel")}
          </Button>
          <Button type="submit" variant="primary" className={button} aria-disabled={busy}>
            {busy ? t("busy") : t("submit")}
          </Button>
        </div>
      </form>
    </dialog>
  );
}
