"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { Choice } from "@/components/catalog/product-create";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { Button } from "@/components/ui/button";
import {
  getPriceListsListQueryKey,
  getPromotionsListQueryKey,
  priceListsList,
  promotionsList,
} from "@/lib/api/client";
import type { Promotion, PromotionStatusEnum } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { PromotionPanel } from "./promotion-panel";
import { stamp, ZONE } from "./zoned";

const STATUSES = ["DRAFT", "PUBLISHED", "CANCELLED"] as const;

// Las promociones (F3-48f): `GET …/pricing/promotions/`, por cursor. La API no filtra por estado:
// el filtro corta las filas traídas. Nada se calcula aquí (ADR-006).
export function Promotions() {
  const t = useTranslations("promotions");
  const format = useFormatter();
  const zone = useTimeZone() ?? ZONE;
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  // Comodidad: las acciones se ofrecen a quien la API dijo que tiene `prices.manage`.
  const canManage = permissions.some((grant) => grant.code === "prices.manage");
  const list = useRef<CursorListHandle>(null);
  const [status, setStatus] = useState<"" | PromotionStatusEnum>("");
  const lists = useQuery({
    queryKey: [...getPriceListsListQueryKey(slug), "all", "names"],
    queryFn: async ({ signal }) =>
      new Map(
        (await everyPage((params) => priceListsList(slug, params, { signal }))).map(
          (row) => [row.id, row.name] as const,
        ),
      ),
    gcTime: 0,
  });
  const listNames = lists.data ?? new Map<string, string>();
  const money = (amount: string, currency: string) =>
    format.number(Number(amount), { style: "currency", currency });
  const discount = (type: Promotion["discount_type"], value: string, currency: string) =>
    t(`discount.${type}`, {
      value: type === "PERCENT_OFF" ? format.number(Number(value)) : money(value, currency),
    });

  return (
    <CursorList<Promotion>
      ref={list}
      section="promotions"
      organization={organization.name}
      listKey={[...getPromotionsListQueryKey(slug), "pages", status]}
      fetchPage={async (cursor, signal) => {
        const page = await promotionsList(slug, cursor ? { cursor } : undefined, { signal });
        return { ...page, results: page.results.filter((row) => !status || row.status === status) };
      }}
      controls={
        <Choice
          label={t("filter")}
          value={status}
          busy={false}
          onChange={(value) => setStatus(value as typeof status)}
          options={[
            ["", t("all")],
            ...STATUSES.map((one): [string, string] => [one, t(`status.${one}`)]),
          ]}
        />
      }
      empty={<p className="text-muted">{t(status ? "emptyFilter" : "empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(row) => (
        <Row
          promotion={row}
          slug={slug}
          canManage={canManage}
          listNames={listNames}
          discount={discount}
          window={t("window", { from: stamp(row.starts_at, zone), to: stamp(row.ends_at, zone) })}
          onChanged={() => list.current?.refetch()}
        />
      )}
    </CursorList>
  );
}

type RowProps = Pick<
  Parameters<typeof PromotionPanel>[0],
  "slug" | "canManage" | "listNames" | "discount"
> & { promotion: Promotion; window: string; onChanged: () => void };

// Una fila y, si se abre, su panel. Lo que respondió una acción (el estado nuevo) manda sobre
// la fila hasta que la lista, vuelta a pedir, trae una versión igual o más nueva.
function Row({ promotion, window, onChanged, ...rest }: RowProps) {
  const t = useTranslations("promotions");
  const [open, setOpen] = useState(false);
  const [fresh, setFresh] = useState<Promotion | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const p = fresh && fresh.version > promotion.version ? fresh : promotion;
  const names = p.price_list_ids.map((id) => rest.listNames.get(id) ?? `#${id.slice(0, 8)}`);
  return (
    <>
      <p className="flex min-w-0 flex-col leading-snug">
        <span className="font-medium wrap-anywhere">{p.name}</span>
        <span className="text-sm tabular-nums">
          {rest.discount(p.discount_type, p.value, p.currency)}
        </span>
        <span className="text-muted text-sm tabular-nums">{window}</span>
        <span className="text-muted text-sm wrap-anywhere">
          {t("lists", { names: names.join(", ") })}
        </span>
      </p>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm sm:flex-col sm:items-end">
        {/* Con texto, no solo con color: el estado se lee igual sin distinguir colores. */}
        <span
          className={cn(
            "rounded-full border px-2.5 py-0.5 font-medium",
            p.status === "PUBLISHED" && "border-success text-success",
            p.status === "CANCELLED" && "border-border text-muted line-through",
            p.status === "DRAFT" && "border-foreground/40",
          )}
        >
          {t(`status.${p.status}`)}
        </span>
        <span className="text-muted tabular-nums">{t("priority", { priority: p.priority })}</span>
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-haspopup="dialog"
          aria-label={t("openLabel", { name: p.name })}
          onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
          onClick={() => setOpen(true)}
        >
          {t("open")}
        </Button>
      </p>
      {open ? (
        <PromotionPanel
          {...rest}
          promotion={p}
          onChanged={(next) => {
            setFresh(next);
            onChanged();
          }}
          onClose={() => {
            setOpen(false);
            trigger.current?.focus();
          }}
        />
      ) : null}
    </>
  );
}
