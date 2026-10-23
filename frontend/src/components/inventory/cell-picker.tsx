"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { everyPage } from "@/components/catalog/categories-tree";
import { Choice } from "@/components/catalog/product-create";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import {
  getProductsListQueryKey,
  getVariantsListQueryKey,
  getWarehousesListQueryKey,
  productsList,
  variantsList,
  warehousesList,
} from "@/lib/api/client";
import type { Warehouse } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

// Para buscar sin mayúsculas ni tildes: «bano» encuentra «Baño».
const plain = (text: string) =>
  text.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase("es-PE");
type Read = { isError: boolean; error: unknown; refetch: () => unknown };
export const statusOf = (read: Read) => (read.error as ApiError | null)?.status;
// Lo que pide `stock.warehouseOption`: nombre, código y si está activo.
export const placed = ({ name, code, is_active }: Warehouse) => ({
  ...{ name, code },
  active: String(is_active),
});

// Lo que no llegó, por `code`, y su reintento. Sin sesión (401) nada: `Providers` lleva al
// login. Un 403 se explica con `denied` y no se reintenta.
export function useReads() {
  const errors = useTranslations("errors");
  const failed = (read: Read, denied: string) => {
    const status = statusOf(read);
    if (!read.isError || status === 401) return undefined;
    return status === 403 ? denied : errors(`api.${apiErrorKey(read.error)}`);
  };
  const retry = (read: Read) =>
    read.isError && statusOf(read) !== 401 && statusOf(read) !== 403 ? (
      <Button
        variant="ghost"
        className="border-border min-h-11 self-start border sm:min-h-9"
        onClick={() => void read.refetch()}
      >
        {errors("retry")}
      </Button>
    ) : null;
  return { failed, retry };
}

// Elegir una celda del inventario (F3-14): el almacén y la variante (buscando el producto). La
// usan las existencias y el historial. Almacenes, productos y variantes se piden enteros
// (`everyPage`): la API de productos no busca, así que la búsqueda es del navegador. Nada queda
// entre visitas (`gcTime: 0`). `anyWarehouse`: «Todos los almacenes» también vale (F3-17).
export function useCellPicker(slug: string, anyWarehouse = false) {
  const t = useTranslations("stock");
  const { failed, retry } = useReads();
  const [warehouseId, setWarehouseId] = useState("");
  const [search, setSearch] = useState("");
  const [productId, setProductId] = useState("");
  const [variantId, setVariantId] = useState("");
  const warehouses = useQuery({
    queryKey: [...getWarehousesListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => warehousesList(slug, params, { signal })),
    gcTime: 0,
  });
  // Solo los que llevan stock: la API no acepta movimientos de un servicio.
  const products = useQuery({
    queryKey: [...getProductsListQueryKey(slug), "all"],
    queryFn: async ({ signal }) =>
      (await everyPage((params) => productsList(slug, params, { signal }))).filter(
        (row) => row.tracks_inventory,
      ),
    gcTime: 0,
  });
  const variants = useQuery({
    queryKey: [...getVariantsListQueryKey(slug, productId), "all"],
    queryFn: ({ signal }) =>
      everyPage((params) => variantsList(slug, productId, params, { signal })),
    enabled: !!productId,
    gcTime: 0,
  });
  // Una variante única se elige sola; lo elegido que ya no está en su lista no cuenta.
  const variant =
    variants.data?.find((row) => row.id === variantId) ??
    (variants.data?.length === 1 ? variants.data[0] : undefined);
  const warehouse = warehouses.data?.find((row) => row.id === warehouseId);
  const wanted = plain(search.trim());
  // Lo elegido sigue en el `select` aunque la búsqueda ya no lo encuentre.
  const shown = (products.data ?? []).filter(
    (row) => row.id === productId || plain(row.name).includes(wanted),
  );
  const hint = (read: Read & { isPending: boolean }, denied: string) =>
    failed(read, denied) ?? (read.isPending ? t("loading") : undefined);

  const controls = (
    <fieldset className="grid gap-4 sm:grid-cols-2">
      <legend className="sr-only">{t("choose")}</legend>
      <div className="flex flex-col gap-2">
        <Choice
          label={t("warehouse")}
          value={warehouse ? warehouseId : ""}
          hint={hint(warehouses, t("denied"))}
          busy={false}
          onChange={setWarehouseId}
          options={[
            ["", t(anyWarehouse ? "anyWarehouse" : "chooseWarehouse")],
            ...(warehouses.data ?? []).map((row): [string, string] => [
              row.id,
              t("warehouseOption", placed(row)),
            ]),
          ]}
        />
        {retry(warehouses)}
      </div>
      <div className="flex flex-col gap-2">
        <TextField
          label={t("search")}
          type="search"
          autoComplete="off"
          value={search}
          hint={wanted && shown.length === 0 ? t("noMatch") : undefined}
          onChange={(event) => setSearch(event.target.value)}
        />
        <Choice
          label={t("product")}
          value={productId}
          hint={hint(products, t("catalogDenied"))}
          busy={false}
          onChange={(id) => {
            setProductId(id);
            setVariantId("");
          }}
          options={[
            ["", t("chooseProduct")],
            ...shown.map((row): [string, string] => [row.id, row.name]),
          ]}
        />
        {retry(products)}
        {productId ? (
          <Choice
            label={t("variant")}
            value={variant?.id ?? ""}
            hint={hint(variants, t("catalogDenied"))}
            busy={false}
            onChange={setVariantId}
            options={[
              ["", t("chooseVariant")],
              ...(variants.data ?? []).map(({ id, name, sku }): [string, string] => [
                id,
                t("variantOption", { name, sku }),
              ]),
            ]}
          />
        ) : null}
        {retry(variants)}
      </div>
    </fieldset>
  );
  return { warehouses, products, warehouse, variant, controls };
}
