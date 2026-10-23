"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import {
  branchesList,
  getBranchesListQueryKey,
  getWarehousesListQueryKey,
  warehousesList,
} from "@/lib/api/client";
import type { Warehouse } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { type Branches, WarehouseForm } from "./warehouse-form";

// Almacenes de la organización (F3-13): lo que devuelve `GET /api/v1/o/{slug}/warehouses/`
// (F3-08), activos o no, por orden de creación. La lista, sus estados y su foco son los de
// `CursorList`. Quién puede verlos o cambiarlos lo decide la API (`inventory.view`,
// `warehouses.manage`).
export function WarehousesList() {
  const t = useTranslations("warehouses");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  // Comodidad: crear y editar se ofrecen a quien la API dijo que tiene `warehouses.manage`.
  const canManage = held("warehouses.manage");
  const canSeeBranches = held("organization.view"); // el permiso con que la API las lista
  const listKey = [...getWarehousesListQueryKey(slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  // Las sucursales, enteras: nombran la de cada almacén y se eligen al crear uno.
  const branches = useQuery({
    queryKey: [...getBranchesListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => branchesList(slug, params, { signal })),
    enabled: canSeeBranches,
    gcTime: 0,
  });
  const choices: Branches =
    branches.data ?? (branches.isError || !canSeeBranches ? "failed" : "loading");
  const named = new Map(branches.data?.map((row) => [row.id, row]));
  // Lo que respondió la última escritura. Un almacén que ya no existe (`alert`) se explica aquí
  // y no en su fila, que desaparece cuando llega la lista nueva.
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  function stale(name: string, here: boolean) {
    setSaid({ text: t("stale", { name }), alert: true });
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const shared = { slug, listKey, branches: choices, onStale: stale };
  const form = (warehouse?: Warehouse) => (
    <WarehouseForm
      {...shared}
      warehouse={warehouse}
      onAsk={() => setSaid(null)}
      onDone={(text) => setSaid({ text })}
    />
  );

  return (
    <CursorList<Warehouse>
      ref={list}
      section="warehouses"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        warehousesList(slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        canManage ? (
          <>
            {form()}
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
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(warehouse) => {
        const branch = named.get(warehouse.branch_id);
        return (
          <>
            <p className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium wrap-anywhere">{warehouse.name}</span>
              <span className="text-muted font-mono text-[13px] wrap-anywhere">
                {warehouse.code}
              </span>
              {branch ? (
                <span className="text-muted text-sm wrap-anywhere">
                  {t("branch", { name: branch.name, code: branch.code })}
                </span>
              ) : null}
            </p>
            <p className="flex flex-wrap gap-x-3 text-sm sm:flex-col sm:items-end">
              <span
                className={cn("font-medium", warehouse.is_active ? "text-success" : "text-muted")}
              >
                {t(warehouse.is_active ? "active" : "inactive")}
              </span>
              <span className="text-muted">{t(`type.${warehouse.type}`)}</span>
              <span className="text-muted">
                {t(warehouse.is_sellable ? "sellable" : "notSellable")}
              </span>
            </p>
            {canManage ? <div className="sm:col-span-2">{form(warehouse)}</div> : null}
          </>
        );
      }}
    </CursorList>
  );
}
