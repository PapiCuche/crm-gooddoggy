"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { type ReactNode, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { Choice } from "@/components/catalog/product-create";
import { useReads } from "@/components/inventory/cell-picker";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import {
  getPriceListPricesListQueryKey,
  getPriceListsListQueryKey,
  getProductsListQueryKey,
  priceListPricesList,
  priceListsList,
  productsList,
} from "@/lib/api/client";
import type { ListPrice } from "@/lib/api/model";

import { PriceForm } from "./price-form";
import { stamp, ZONE } from "./zoned";

// Una fila por variante: la API no da `id` a la fila, así que es el de la variante.
type Row = ListPrice & { id: string };

// Los precios de una lista (F3-45): `GET …/pricing/price-lists/{id}/prices/` (F3-44), una fila
// por variante viva con su precio vigente, el próximo y lo que resuelve `PricingService`. Nada
// se calcula aquí: el importe es el de la API (ADR-006). Se elige la lista entre las activas
// que el actor ve (la ruta de una inactiva es un 404) y se filtra por producto. Con
// `prices.manage`, cada fila ofrece «Cambiar precio»; el historial (F3-44b) irá a su lado.
export function ListPrices() {
  const t = useTranslations("prices");
  const { organization, permissions } = useTenant();
  // Comodidad: «Cambiar precio» se ofrece a quien la API dijo que tiene `prices.manage`.
  const canManage = permissions.some((grant) => grant.code === "prices.manage");
  const list = useRef<CursorListHandle>(null);
  // Lo que respondió la última escritura; un 404 (`alert`) se explica aquí.
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const [chosen, setChosen] = useState("");
  const [productId, setProductId] = useState("");
  const lists = useQuery({
    queryKey: [...getPriceListsListQueryKey(slug), "all"],
    queryFn: async ({ signal }) =>
      (await everyPage((params) => priceListsList(slug, params, { signal })))
        .filter((row) => row.is_active)
        .sort((a, b) => a.sort_order - b.sort_order),
    gcTime: 0,
  });
  const products = useQuery({
    queryKey: [...getProductsListQueryKey(slug), "all", "any"],
    queryFn: ({ signal }) => everyPage((params) => productsList(slug, params, { signal })),
    gcTime: 0,
  });
  const active = lists.data ?? [];
  // Se abre en la predeterminada; lo elegido que ya no está activo no cuenta.
  const priceList =
    active.find((row) => row.id === chosen) ?? active.find((row) => row.is_default) ?? active[0];
  const listId = priceList?.id ?? "";
  const params = productId ? { product_id: productId } : {};
  const listKey = [...getPriceListPricesListQueryKey(slug, listId, params), "pages"];

  let empty = <p className="text-muted">{t(productId ? "emptyProduct" : "empty")}</p>;
  if (!priceList) {
    const why = failed(lists, t("listsDenied"));
    empty = (
      <div className="flex flex-col items-start gap-3">
        <p role={why ? "alert" : "status"} className={why ? "text-danger" : "text-muted"}>
          {why ?? t(lists.isPending ? "loadingLists" : "noLists")}
        </p>
        {retry(lists)}
      </div>
    );
  }

  const form = (row: Row) => (
    <PriceForm
      slug={slug}
      listId={listId}
      listKey={listKey}
      row={row}
      onAsk={() => setSaid(null)}
      onDone={(text) => setSaid({ text })}
      onStale={(here) => {
        // La lista se desactivó o dejó de verse, o la variante ya no está: se pide todo otra vez.
        setSaid({ text: t("stale"), alert: true });
        void lists.refetch();
        list.current?.refetch();
        if (here) list.current?.focusHeading();
      }}
    />
  );

  return (
    <CursorList<Row>
      ref={list}
      section="prices"
      organization={organization.name}
      listKey={listKey}
      fetchPage={async (cursor, signal) => {
        // Sin lista elegida todavía (o ninguna activa), no hay ruta que pedir.
        if (!listId) return { results: [], next: null };
        const page = await priceListPricesList(
          slug,
          listId,
          { ...params, ...(cursor ? { cursor } : {}) },
          { signal },
        );
        return { ...page, results: page.results.map((row) => ({ ...row, id: row.variant.id })) };
      }}
      controls={
        <fieldset className="grid gap-4 sm:grid-cols-2">
          <legend className="sr-only">{t("choose")}</legend>
          <Choice
            label={t("list")}
            value={listId}
            busy={false}
            hint={lists.isPending ? t("loadingLists") : undefined}
            onChange={setChosen}
            options={active.map((row): [string, string] => [
              row.id,
              t("listOption", { name: row.name, default: String(row.is_default) }),
            ])}
          />
          <div className="flex flex-col gap-2">
            <Choice
              label={t("product")}
              value={productId}
              busy={false}
              hint={failed(products, t("productsDenied"))}
              onChange={setProductId}
              options={[
                ["", t("anyProduct")],
                ...(products.data ?? []).map((row): [string, string] => [row.id, row.name]),
              ]}
            />
            {retry(products)}
          </div>
        </fieldset>
      }
      notice={
        canManage ? (
          <>
            {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
            <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
              {said && !said.alert ? said.text : ""}
            </p>
            {said?.alert ? (
              <p role="alert" className="text-danger">
                {said.text}
              </p>
            ) : null}
          </>
        ) : null
      }
      empty={empty}
      rowClassName="grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)] sm:items-start"
    >
      {(row) => <PriceRow row={row} action={canManage ? form(row) : null} />}
    </CursorList>
  );
}

// Una variante: quién es a la izquierda; su precio, desde cuándo, hasta cuándo y el próximo a
// la derecha (debajo en el móvil). Fechas en la zona de la organización.
function PriceRow({ row, action }: { row: Row; action: ReactNode }) {
  const t = useTranslations("prices");
  const format = useFormatter();
  const { variant, current, next, resolution } = row;
  const money = (amount: string) =>
    format.number(Number(amount), { style: "currency", currency: current?.currency ?? "PEN" });
  const zone = useTimeZone() ?? ZONE;
  const when = (iso: string, year = true) => stamp(iso, zone, year);
  const price = resolution.found && resolution.final_amount;

  return (
    <>
      <p className="flex min-w-0 flex-col leading-snug">
        <span className="font-medium wrap-anywhere">{variant.product_name}</span>
        <span className="wrap-anywhere">{variant.name}</span>
        <span className="text-muted font-mono text-[13px] wrap-anywhere">{variant.sku}</span>
      </p>
      <div className="flex min-w-0 flex-col gap-1 text-sm sm:items-end sm:text-right">
        {price ? (
          <p className="flex items-baseline gap-2">
            <span className="text-lg font-semibold tabular-nums">{money(price)}</span>
            <span className="text-muted">{t("taxIncluded")}</span>
          </p>
        ) : (
          <p className="font-medium">{t(`reason.${resolution.reason ?? "NO_PRICE"}`)}</p>
        )}
        {current ? (
          <p className="text-muted tabular-nums">
            {price ? null : `${t("listPrice", { amount: money(current.amount) })} · `}
            {t("since", { at: when(current.starts_at) })}
          </p>
        ) : null}
        {price ? (
          <p className="text-muted tabular-nums">
            {resolution.valid_until
              ? t("until", { at: when(resolution.valid_until) })
              : t("untilOpen")}
          </p>
        ) : null}
        {next ? (
          <p className="font-medium tabular-nums">
            {t("next", { amount: money(next.amount), at: when(next.starts_at, false) })}
          </p>
        ) : null}
      </div>
      {action ? <div className="sm:col-span-2">{action}</div> : null}
    </>
  );
}
