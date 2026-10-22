"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { getInventoryLevelsListQueryKey, inventoryLevelsList } from "@/lib/api/client";

import { placed, statusOf, useCellPicker, useReads } from "./cell-picker";

const ZERO = { on_hand: 0, reserved: 0, available: 0 }; // una celda sin movimientos

// Existencias (F3-14): elegir almacén y variante y ver su saldo (`GET …/inventory/levels/`,
// F3-11) en ese almacén y en cada almacén con movimientos. Quién ve qué lo decide la API
// (`inventory.view`, y `products.view` para elegir el producto).
export function StockPanel() {
  const t = useTranslations("stock");
  const { organization } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const { warehouses, products, warehouse, variant, controls } = useCellPicker(slug);
  const levels = useQuery({
    queryKey: [...getInventoryLevelsListQueryKey(slug, { variant_id: variant?.id }), "all"],
    queryFn: ({ signal }) =>
      everyPage((params) =>
        inventoryLevelsList(slug, { ...params, variant_id: variant!.id }, { signal }),
      ),
    enabled: !!variant,
    gcTime: 0,
  });
  const named = new Map(warehouses.data?.map((row) => [row.id, row]));
  const figures = (cell: typeof ZERO) => (
    <dl className="grid grid-cols-3 gap-2">
      {(["on_hand", "reserved", "available"] as const).map((key) => (
        <div key={key} className="flex flex-col">
          <dt className="text-muted text-sm">{t(key)}</dt>
          <dd className="text-[28px] leading-tight font-bold tabular-nums">{cell[key]}</dd>
        </div>
      ))}
    </dl>
  );

  let body;
  if (statusOf(warehouses) === 403) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (warehouses.data?.length === 0 || products.data?.length === 0) {
    // Sin almacenes o sin productos no hay nada que elegir: se dice qué falta y dónde se crea.
    const missing = warehouses.data?.length === 0 ? "warehouses" : "products";
    const path = missing === "warehouses" ? "inventario/almacenes" : "catalogo/productos";
    body = (
      <p className="text-muted">
        {t(`no_${missing}`)}{" "}
        <Link href={`/o/${slug}/${path}`} className="text-foreground underline">
          {t(`create_${missing}`)}
        </Link>
      </p>
    );
  } else {
    let cell;
    if (!variant || !warehouse) {
      cell = <p className="text-muted">{t("pick")}</p>;
    } else if (levels.isPending) {
      cell = (
        <p role="status" className="text-muted">
          {t("loadingLevels")}
        </p>
      );
    } else if (levels.isError) {
      cell = (
        <div className="flex flex-col gap-3">
          <p role="alert" className="text-danger">
            {failed(levels, t("denied"))}
          </p>
          {retry(levels)}
        </div>
      );
    } else {
      const here = levels.data.find((row) => row.warehouse_id === warehouse.id) ?? ZERO;
      cell = (
        <>
          <section className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4">
            <h2 className="font-medium wrap-anywhere">
              {t("here", { variant: variant.name, warehouse: warehouse.name })}
            </h2>
            {figures(here)}
          </section>
          <section className="flex flex-col gap-2">
            <h2 className="font-medium">{t("everywhere")}</h2>
            {levels.data.length === 0 ? (
              <p className="text-muted">{t("none")}</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {levels.data.map((row) => {
                  const where = named.get(row.warehouse_id);
                  return (
                    <li key={row.id} className="border-border bg-surface rounded-lg border p-4">
                      <p className="mb-2 text-sm font-medium wrap-anywhere">
                        {where ? t("warehouseOption", placed(where)) : t("unknownWarehouse")}
                      </p>
                      {figures(row)}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </>
      );
    }
    body = (
      <>
        {controls}
        {cell}
      </>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {body}
    </section>
  );
}
