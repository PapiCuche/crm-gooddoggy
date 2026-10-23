"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { getPriceListsListQueryKey, priceListsList } from "@/lib/api/client";
import type { PriceList } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { audienceKey, PriceListForm } from "./price-list-form";

// Listas de precios de la organización (F3-41): lo que devuelve `GET …/pricing/price-lists/`
// (F3-40b), activas o no, por orden de creación. La API solo trae las que el actor ve (su
// `required_permission`, o todas con `prices.manage`). La lista, sus estados y su foco son los
// de `CursorList`.
export function PriceLists() {
  const t = useTranslations("priceLists");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  // Comodidad: crear y editar se ofrecen a quien la API dijo que tiene `prices.manage`.
  const canManage = permissions.some((grant) => grant.code === "prices.manage");
  const listKey = [...getPriceListsListQueryKey(slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  // Lo que respondió la última escritura. Una lista que ya no se ve (`alert`) se explica aquí.
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  function stale(name: string, here: boolean) {
    setSaid({ text: t("stale", { name }), alert: true });
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const form = (priceList?: PriceList) => (
    <PriceListForm
      slug={slug}
      listKey={listKey}
      priceList={priceList}
      onAsk={() => setSaid(null)}
      onDone={(text) => setSaid({ text })}
      onStale={stale}
    />
  );

  return (
    <CursorList<PriceList>
      ref={list}
      section="priceLists"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        priceListsList(slug, cursor ? { cursor } : undefined, { signal })
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
      {(row) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{row.name}</span>
            <span className="text-muted font-mono text-[13px] wrap-anywhere">{row.code}</span>
            <span className="text-muted text-sm">
              {t(`kind.${row.kind}`)} · {row.currency} ·{" "}
              {t(`payment.${row.payment_condition ?? "none"}`)}
            </span>
            <span className="text-sm">
              {t("seenBy", { audience: t(`audience.${audienceKey(row.required_permission)}`) })}
            </span>
          </p>
          <p className="flex flex-wrap gap-x-3 text-sm sm:flex-col sm:items-end">
            {row.is_default ? <span className="font-medium">{t("default")}</span> : null}
            <span className={cn("font-medium", row.is_active ? "text-success" : "text-muted")}>
              {t(row.is_active ? "active" : "inactive")}
            </span>
            <span className="text-muted tabular-nums">{t("order", { order: row.sort_order })}</span>
          </p>
          {canManage ? <div className="sm:col-span-2">{form(row)}</div> : null}
        </>
      )}
    </CursorList>
  );
}
