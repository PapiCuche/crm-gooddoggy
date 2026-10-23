"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { usePriceListsCreate, usePriceListsUpdate } from "@/lib/api/client";
import {
  type PriceList,
  PriceListKindEnum,
  PriceListPaymentConditionEnum,
  PricePermissionEnum,
} from "@/lib/api/model";
import { cn } from "@/lib/utils";

type Kind = PriceListKindEnum;
type Permission = PricePermissionEnum;
type Payment = PriceListPaymentConditionEnum;
// Lo que se elige fuera de los campos de texto, fijado al abrir. `required_permission` en `null`
// (solo al crear): sale del tipo, como en la API, hasta que se elige otro.
type Picked = Pick<PriceList, "kind" | "payment_condition" | "is_active" | "is_default"> & {
  required_permission: Permission | null;
};
const FRESH: Picked = {
  ...{ kind: "RETAIL", required_permission: null, payment_condition: null },
  ...{ is_active: true, is_default: false },
};
// El permiso que la API pone si no se envía (F3-40).
const FROM_KIND: Partial<Record<Kind, Permission>> = {
  WHOLESALE: "prices.view_wholesale",
  DISTRIBUTOR: "prices.view_distributor",
  INTERNAL: "prices.view_internal",
};
// El texto legible de un permiso de lectura: su clave en `priceLists.audience`.
export const audienceKey = (code: string) => code.replace("prices.", "") as "view";
export const permissionFor = (kind: Kind): Permission => FROM_KIND[kind] ?? "prices.view";
type Text = "code" | "name" | "sort_order";
const ORDER = /^\d{1,4}$/; // 0…9999, como la API

// Crear una lista de precios (sin `priceList`) o cambiar una (F3-41), con `POST` y `PATCH
// …/pricing/price-lists/` (F3-40b). El código y la moneda no cambian: la API responde 400 si un
// `PATCH` los trae, así que editar no los envía, y envía solo lo que cambió. El camino de envío
// es el de `FieldsForm`. Quién puede hacerlo lo decide la API (`prices.manage`).
export function PriceListForm(props: {
  slug: string;
  listKey: QueryKey;
  priceList?: PriceList;
  onAsk: () => void; // se abre el formulario: los avisos anteriores ya no aplican
  onDone: (text: string) => void;
  onStale: (name: string, here: boolean) => void; // `here`: el foco seguía en esta fila
}) {
  const { slug, listKey, priceList } = props;
  const t = useTranslations("priceLists.form");
  const named = useTranslations("priceLists");
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Picked | null>(null); // `null`: cerrado
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const base = useRef(priceList); // la fila al abrir: con ella se compara lo que cambió

  async function saved(row: PriceList) {
    // Sigue «Guardando…» hasta que la lista vuelve (otra pudo dejar de ser la predeterminada).
    await queryClient.invalidateQueries({ queryKey: listKey });
    const stale = queryClient.getQueryState(listKey)?.status === "error";
    setPicked(null);
    props.onDone(t(stale ? "doneStale" : "done", { name: row.name, created: String(!priceList) }));
  }
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = usePriceListsCreate({ mutation: { networkMode: "always", onSuccess: saved } });
  const update = usePriceListsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: saved,
      onError: (error) => {
        // La lista ya no existe o ya no se ve: lo explica la vista, que se vuelve a pedir.
        if (error.status !== 404 || !priceList) return;
        const active = document.activeElement;
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        setPicked(null);
        props.onStale(priceList.name, here);
      },
    },
  });
  const write = priceList ? update : create;
  const busy = write.isPending;

  const opened = useRef(false);
  const open = picked !== null;
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
    base.current = priceList;
    const { kind, required_permission, payment_condition, is_active, is_default } =
      priceList ?? FRESH;
    const fixed = { kind, required_permission, payment_condition, is_active, is_default };
    setPicked(next ? fixed : null);
  }
  const pick = (change: Partial<Picked>) => setPicked((now) => now && { ...now, ...change });
  const title = priceList ? t("editLabel", { name: priceList.name }) : t("title");
  const id = useId();
  // «IA pública» (`ai`) se enseña sin poder cambiarse: no tiene efecto hasta la Fase 8.
  const toggle = (name: "is_active" | "is_default" | "ai", hint?: string) => (
    <div className="flex flex-col gap-1">
      <label className="has-disabled:text-muted flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
        <input
          type="checkbox"
          className="size-4 shrink-0"
          checked={name === "ai" ? (priceList?.ai_exposable ?? false) : picked![name]}
          disabled={name === "ai"}
          aria-disabled={busy}
          aria-describedby={hint ? `${id}-${name}` : undefined}
          onChange={() => busy || name === "ai" || pick({ [name]: !picked![name] })}
        />
        {t(name)}
      </label>
      {hint ? (
        <p id={`${id}-${name}`} className="text-muted text-sm">
          {hint}
        </p>
      ) : null}
    </div>
  );
  const options = (group: "kind" | "audience" | "payment", codes: string[]) =>
    codes.map((code): [string, string] => [
      code,
      named(`${group}.${group === "audience" ? audienceKey(code) : code}` as "kind.RETAIL"),
    ]);
  const fields: Text[] = priceList ? ["name", "sort_order"] : ["code", "name", "sort_order"];

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-3", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {picked ? (
        <FieldsForm
          title={title}
          heading={<h2 className="font-medium wrap-anywhere">{title}</h2>}
          className={cn(
            "flex w-full max-w-xl flex-col gap-4",
            !priceList && "border-border bg-surface rounded-lg border p-4",
          )}
          fields={fields.map((name) => ({
            name,
            max: { code: 30, name: 100, sort_order: 4 }[name],
            label: t(name),
            required: t(`${name}Required`),
            invalid: t(`${name}Invalid`),
            hint: name === "name" ? undefined : t(`${name}Hint`),
            literal: name === "code",
            inputMode: name === "sort_order" ? "numeric" : undefined,
            valid: name === "sort_order" ? (value) => ORDER.test(value) : undefined,
            defaultValue:
              name === "code" ? undefined : `${priceList?.[name] ?? (name === "name" ? "" : 0)}`,
          }))}
          note={
            <>
              <Choice
                label={t("kind")}
                value={picked.kind}
                busy={busy}
                onChange={(kind) => pick({ kind: kind as Kind })}
                options={options("kind", Object.values(PriceListKindEnum))}
              />
              <Choice
                label={t("audience")}
                value={picked.required_permission ?? permissionFor(picked.kind)}
                busy={busy}
                hint={picked.required_permission ? undefined : t("audienceHint")}
                onChange={(code) => pick({ required_permission: code as Permission })}
                options={options("audience", Object.values(PricePermissionEnum))}
              />
              <Choice
                label={t("payment")}
                value={picked.payment_condition ?? "none"}
                busy={busy}
                onChange={(code) =>
                  pick({ payment_condition: code === "none" ? null : (code as Payment) })
                }
                options={options("payment", [
                  "none",
                  ...Object.values(PriceListPaymentConditionEnum),
                ])}
              />
              {toggle("is_active")}
              {toggle("is_default", priceList ? undefined : t("defaultHint"))}
              {toggle("ai", t("aiHint"))}
            </>
          }
          write={write}
          send={({ code, name, sort_order }) => {
            const { required_permission, is_default, ...rest } = picked;
            const chosen = { name, sort_order: Number(sort_order), ...rest };
            if (!priceList) {
              // Sin `required_permission` ni `is_default`, decide la API (del tipo; la primera
              // activa que ven todos es la predeterminada).
              const data = {
                code,
                ...chosen,
                ...(required_permission && { required_permission }),
                ...(is_default && { is_default }),
              };
              return create.mutate({ orgSlug: slug, data });
            }
            // Solo lo que cambió respecto a la fila: no pisa lo que otro cambió en lo demás.
            const data = Object.fromEntries(
              Object.entries({ ...chosen, required_permission, is_default }).filter(
                ([key, value]) => value !== base.current![key as "name"],
              ),
            );
            if (Object.keys(data).length === 0) return show(false); // nada que guardar
            update.mutate({ orgSlug: slug, priceListId: priceList.id, data });
          }}
          taken={(error) => {
            if (error.code === "PRICE_LIST_CODE_TAKEN")
              return { field: "code", text: t("codeTaken") };
            if (error.code === "PRICE_LIST_IS_DEFAULT") return { text: t("isDefault") };
            return error.fields?.is_default ? { text: t("defaultRestricted") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t(priceList ? "save" : "open"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant={priceList ? "ghost" : "accent"}
          className={cn("min-h-11 sm:min-h-9", priceList && "border-border border")}
          aria-label={priceList ? t("editLabel", { name: priceList.name }) : undefined}
          onClick={() => show(true)}
        >
          {t(priceList ? "edit" : "open")}
        </Button>
      )}
    </div>
  );
}
