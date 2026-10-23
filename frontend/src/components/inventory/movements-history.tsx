"use client";

import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { getInventoryMovementsListQueryKey, inventoryMovementsList } from "@/lib/api/client";
import type { Movement } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { placed, useCellPicker } from "./cell-picker";

// El historial de movimientos (F3-17): el ledger de la variante elegida, en un almacén o en
// todos, del más reciente al más antiguo (`GET …/inventory/movements/`, F3-11), página a página
// con `CursorList`. Sin variante elegida no se pide nada: la API solo devuelve `variant_id`, y
// una fila sin nombre no le sirve a nadie. Quién lo ve lo decide la API (`inventory.view`).
export function MovementsHistory() {
  const t = useTranslations("ledger");
  const format = useFormatter();
  const { organization } = useTenant();
  const slug = organization.slug;
  const { warehouses, warehouse, variant, controls } = useCellPicker(slug, true);
  const named = new Map(warehouses.data?.map((row) => [row.id, row]));
  const filters = {
    ...(variant ? { variant_id: variant.id } : {}),
    ...(warehouse ? { warehouse_id: warehouse.id } : {}),
  };
  // Cada combinación es su propia lista; los filtros viajan también con «Cargar más».
  const listKey = [...getInventoryMovementsListQueryKey(slug, filters), "pages"];

  return (
    <CursorList<Movement>
      section="ledger"
      organization={organization.name}
      listKey={listKey}
      fetchPage={async (cursor, signal) =>
        variant
          ? inventoryMovementsList(slug, { ...filters, ...(cursor ? { cursor } : {}) }, { signal })
          : { results: [], next: null }
      }
      controls={controls}
      empty={<p className="text-muted">{t(variant ? "empty" : "pick")}</p>}
      rowClassName="grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(movement) => {
        const where = named.get(movement.warehouse_id);
        const change = movement.quantity;
        return (
          <>
            <p className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium">{t(`types.${movement.movement_type}`)}</span>
              <span className="wrap-anywhere">{movement.reason}</span>
              <span className="text-muted text-sm wrap-anywhere">
                {where ? t("warehouse", placed(where)) : t("unknownWarehouse")}
              </span>
            </p>
            <p className="flex flex-wrap gap-x-3 text-sm sm:flex-col sm:items-end sm:text-right">
              <span className={cn("font-medium tabular-nums", change > 0 && "text-success")}>
                <span aria-hidden>
                  {change > 0 ? `+${change}` : change < 0 ? `−${-change}` : "0"}
                </span>
                <span className="sr-only">{t("change", { change })}</span>
              </span>
              <span className="tabular-nums">
                {t("after", { on_hand: movement.on_hand_after })}
              </span>
              <time className="text-muted" dateTime={movement.created_at}>
                {format.dateTime(new Date(movement.created_at), {
                  dateStyle: "medium",
                  timeStyle: "short",
                })}
              </time>
            </p>
          </>
        );
      }}
    </CursorList>
  );
}
