"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { CursorList } from "@/components/lists/cursor-list";
import { Button } from "@/components/ui/button";
import { getInventoryLevelsListQueryKey, inventoryLevelsList } from "@/lib/api/client";
import type { Level } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { placed, statusOf, useCellPicker, useReads } from "./cell-picker";
import { MovementForm } from "./movement-form";

const ZERO = { on_hand: 0, reserved: 0, available: 0 }; // una celda sin movimientos

// Existencias (F3-14): elegir almacén y variante y ver su saldo (`GET …/inventory/levels/`,
// F3-11) en ese almacén y en cada almacén con movimientos. Con el almacén elegido, debajo, su
// stock entero página a página, con el SKU y el nombre de cada variante (F3-19): elegir una fila
// abre su celda. Quién ve qué lo decide la API (`inventory.view`, y `products.view` para elegir
// el producto). Registrar movimientos: F3-15.
export function StockPanel() {
  const t = useTranslations("stock");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  // Comodidad: registrar se ofrece a quien la API dijo que tiene `inventory.adjust` (F3-15).
  const canAdjust = permissions.some((grant) => grant.code === "inventory.adjust");
  const [done, setDone] = useState<string | null>(null);
  const keyRef = useRef<string | null>(null); // la de `MovementForm`, hasta un registro
  const [zeros, setZeros] = useState(false);
  const cellBox = useRef<HTMLDivElement>(null);
  const { failed, retry } = useReads();
  const { warehouses, products, warehouse, variant, controls, choose } = useCellPicker(slug);
  const levels = useQuery({
    queryKey: [...getInventoryLevelsListQueryKey(slug, { variant_id: variant?.id }), "all"],
    queryFn: ({ signal }) =>
      everyPage((params) =>
        inventoryLevelsList(slug, { ...params, variant_id: variant!.id }, { signal }),
      ),
    enabled: !!variant,
    gcTime: 0,
  });
  // El almacén entero, sin las filas que se quedaron en cero salvo que se pidan. Cada
  // combinación es su propia lista; los filtros viajan también con «Cargar más».
  const filters = {
    ...(warehouse ? { warehouse_id: warehouse.id } : {}),
    ...(zeros ? {} : { has_stock: true }),
  };
  const named = new Map(warehouses.data?.map((row) => [row.id, row]));
  const figures = (cell: typeof ZERO, compact = false) => (
    <dl className="grid grid-cols-3 gap-2">
      {(["on_hand", "reserved", "available"] as const).map((key) => (
        <div key={key} className="flex min-w-0 flex-col">
          <dt className="text-muted text-sm">{t(key)}</dt>
          {/* A 390 px, siete cifras caben en un tercio a 22 px; a 28 px no. */}
          <dd
            className={cn(
              "leading-tight font-bold wrap-anywhere tabular-nums",
              compact ? "text-lg" : "text-[22px] sm:text-[28px]",
            )}
          >
            {cell[key]}
          </dd>
        </div>
      ))}
    </dl>
  );

  let blocked: ReactNode;
  if (statusOf(warehouses) === 403) {
    blocked = <p role="alert">{t("denied")}</p>;
  } else if (warehouses.data?.length === 0 || products.data?.length === 0) {
    // Sin almacenes o sin productos no hay nada que elegir: se dice qué falta y dónde se crea.
    const missing = warehouses.data?.length === 0 ? "warehouses" : "products";
    const path = missing === "warehouses" ? "inventario/almacenes" : "catalogo/productos";
    blocked = (
      <p className="text-muted">
        {t(`no_${missing}`)}{" "}
        <Link href={`/o/${slug}/${path}`} className="text-foreground underline">
          {t(`create_${missing}`)}
        </Link>
      </p>
    );
  }

  let cell;
  if (!variant || !warehouse) {
    cell = <p className="text-muted">{t(warehouse ? "pickRow" : "pick")}</p>;
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
            <ul aria-label={t("everywhere")} className="flex flex-col gap-2">
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

  // Los controles no se desmontan cuando la lista del almacén cambia, falla o se cierra.
  const top = blocked ?? (
    <>
      {controls}
      {/* Elegir una fila trae aquí el foco: lo que hay de esa variante y, debajo, su formulario. */}
      <div ref={cellBox} tabIndex={-1} className="flex flex-col gap-6 focus-visible:outline-none">
        {cell}
      </div>
      {canAdjust ? (
        <>
          {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
          <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
            {done ?? ""}
          </p>
          {variant && warehouse ? (
            <MovementForm
              slug={slug}
              warehouse={warehouse}
              variant={variant}
              keyRef={keyRef}
              onDone={setDone}
            />
          ) : null}
        </>
      ) : null}
      {warehouse ? (
        <div className="flex flex-col gap-1">
          <h2 className="font-medium wrap-anywhere">
            {t("inWarehouse", { warehouse: warehouse.name })}
          </h2>
          <label className="flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
            <input
              type="checkbox"
              className="size-4 shrink-0"
              checked={zeros}
              onChange={() => setZeros(!zeros)}
            />
            {t("zeros")}
          </label>
        </div>
      ) : null}
    </>
  );

  return (
    <CursorList<Level>
      section="stock"
      organization={organization.name}
      listKey={[...getInventoryLevelsListQueryKey(slug, filters), "pages"]}
      fetchPage={async (cursor, signal) =>
        warehouse && !blocked
          ? inventoryLevelsList(slug, { ...filters, ...(cursor ? { cursor } : {}) }, { signal })
          : { results: [], next: null }
      }
      controls={top}
      empty={
        blocked ? (
          <span hidden />
        ) : (
          <p className="text-muted">{t(warehouse ? "emptyWarehouse" : "pickWarehouse")}</p>
        )
      }
      rowClassName="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)_auto] sm:items-center"
    >
      {({ variant: item, ...row }) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{item.name}</span>
            <span className="text-muted text-sm wrap-anywhere">
              {item.is_deleted ? t("deletedSku", { sku: item.sku }) : item.sku}
            </span>
          </p>
          {figures(row, true)}
          {item.is_deleted ? null : (
            <Button
              variant="ghost"
              className="border-border min-h-11 justify-self-start border sm:min-h-9"
              onClick={() => {
                choose(item);
                cellBox.current?.focus();
              }}
            >
              {t(canAdjust ? "openAdjust" : "open")}
              <span className="sr-only">{`: ${item.name}`}</span>
            </Button>
          )}
        </>
      )}
    </CursorList>
  );
}
