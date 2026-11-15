"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { getInventoryMovementsListQueryKey, inventoryMovementsList } from "@/lib/api/client";
import type { Movement, Warehouse } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { placed, useCellPicker } from "./cell-picker";

// El historial de movimientos (F3-17): el ledger, del más reciente al más antiguo (`GET
// …/inventory/movements/`, F3-11), página a página con `CursorList`: de toda la organización, de
// un almacén, de una variante o de los dos. Cada fila nombra su variante con el SKU que trae la
// API (F3-19), también si ya se borró del catálogo. Quién lo ve lo decide la API
// (`inventory.view`). Las dos patas de una transferencia dicen las dos su ruta (F3-32).
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
      fetchPage={(cursor, signal) =>
        inventoryMovementsList(slug, { ...filters, ...(cursor ? { cursor } : {}) }, { signal })
      }
      controls={controls}
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(movement, rows) => {
        const where = named.get(movement.warehouse_id);
        const change = movement.quantity;
        return (
          <>
            <p className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium">{t(`types.${movement.movement_type}`)}</span>
              <span className="wrap-anywhere">
                {t("variantLabel", {
                  name: movement.variant.name,
                  sku: movement.variant.sku,
                  deleted: String(movement.variant.is_deleted),
                })}
              </span>
              <span className="text-muted text-sm wrap-anywhere">{movement.reason}</span>
              <span className="text-muted text-sm wrap-anywhere">
                {movement.transfer_group_id ? (
                  <TransferRoute leg={movement} rows={rows} named={named} slug={slug} />
                ) : where ? (
                  t("warehouse", placed(where))
                ) : (
                  t("unknownWarehouse")
                )}
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

// «Origen → destino» de una pata de transferencia. La otra pata suele estar en la lista (se
// registraron juntas); si un filtro o la paginación la dejan fuera, se pide su transferencia
// (`?transfer_group_id=`, F3-31). Mientras tanto, o si no llega, el otro lado no se nombra.
function TransferRoute({
  leg,
  rows,
  named,
  slug,
}: {
  leg: Movement;
  rows: readonly Movement[];
  named: Map<string, Warehouse>;
  slug: string;
}) {
  const t = useTranslations("ledger");
  const group = leg.transfer_group_id!;
  const pair = (list: readonly Movement[]) =>
    list.find((row) => row.transfer_group_id === group && row.id !== leg.id);
  const loaded = pair(rows);
  const params = { transfer_group_id: group };
  const legs = useQuery({
    queryKey: getInventoryMovementsListQueryKey(slug, params),
    queryFn: ({ signal }) => inventoryMovementsList(slug, params, { signal }),
    enabled: !loaded,
    staleTime: Infinity, // las patas no cambian
  });
  const other = loaded ?? pair(legs.data?.results ?? []);
  const name = (id: string | undefined) => {
    const where = id ? named.get(id) : undefined;
    return where ? t("warehouse", placed(where)) : id ? t("unknownWarehouse") : "…";
  };
  const out = leg.movement_type === "TRANSFER_OUT";
  const [from, to] = out ? [leg, other] : [other, leg];
  return t("route", { from: name(from?.warehouse_id), to: name(to?.warehouse_id) });
}
