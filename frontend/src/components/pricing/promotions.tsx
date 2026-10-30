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

import { PromotionForm } from "./promotion-form";
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
  const canManage = permissions.some((grant) => grant.code === "prices.manage");
  const list = useRef<CursorListHandle>(null);
  const [opened, setOpened] = useState<Promotion | null>(null); // el panel vive fuera de las filas
  const [creating, setCreating] = useState(false);
  const [said, setSaid] = useState("");
  const create = useRef<HTMLButtonElement>(null);
  const [status, setStatus] = useState<"" | PromotionStatusEnum>("");
  const lists = useQuery({
    queryKey: [...getPriceListsListQueryKey(slug), "all", "names"],
    queryFn: ({ signal }) => everyPage((params) => priceListsList(slug, params, { signal })),
    gcTime: 0,
  });
  const listNames = new Map((lists.data ?? []).map((row) => [row.id, row.name] as const));
  const money = (amount: string, currency: string) => {
    try {
      return format.number(Number(amount), { style: "currency", currency });
    } catch {
      return `${amount} ${currency}`; // una moneda que Intl no conoce no tumba la pantalla
    }
  };
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
        <>
          {canManage ? (
            <>
              <Button
                ref={create}
                variant="accent"
                className="min-h-11 sm:min-h-9"
                aria-haspopup="dialog"
                onClick={() => {
                  setSaid("");
                  setCreating(true);
                }}
              >
                {t("form.open")}
              </Button>
              {/* Montado desde el principio: un lector de pantalla lo anuncia al cambiar. */}
              <p role="status" className={said ? "text-sm" : "sr-only"}>
                {said}
              </p>
            </>
          ) : null}
          {creating ? (
            <PromotionForm
              slug={slug}
              priceLists={(lists.data ?? []).filter((row) => row.is_active)}
              onClose={() => {
                setCreating(false);
                requestAnimationFrame(() => create.current?.focus());
              }}
              onCreated={(made) => {
                setCreating(false);
                setSaid(t("form.created"));
                list.current?.refetch();
                setOpened(made); // el detalle, para publicarla
              }}
            />
          ) : null}
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
          {opened ? (
            <PromotionPanel
              slug={slug}
              promotion={opened}
              canManage={canManage}
              listNames={listNames}
              discount={discount}
              onChanged={(next) => {
                setOpened(next);
                list.current?.refetch(); // una sola relectura de la lista
              }}
              onClose={() => {
                const id = opened.id;
                setOpened(null);
                // La fila puede haberse vuelto a pintar tras la relectura: se busca por su id.
                requestAnimationFrame(() => {
                  const row = document.querySelector<HTMLElement>(`[data-promotion="${id}"]`);
                  if (row) row.focus();
                  else list.current?.focusHeading();
                });
              }}
            />
          ) : null}
        </>
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(row) => (
        <Row
          promotion={row}
          listNames={listNames}
          discount={discount}
          window={t("window", {
            ...{ from: stamp(row.starts_at, zone), to: stamp(row.ends_at, zone), zone },
          })}
          onOpen={() => setOpened(row)}
        />
      )}
    </CursorList>
  );
}

type RowProps = Pick<Parameters<typeof PromotionPanel>[0], "listNames" | "discount"> & {
  promotion: Promotion;
  window: string;
  onOpen: () => void;
};

function Row({ promotion: p, window, onOpen, listNames, discount }: RowProps) {
  const t = useTranslations("promotions");
  const names = p.price_list_ids.map((id) => listNames.get(id) ?? `#${id.slice(0, 8)}`);
  return (
    <>
      <p className="flex min-w-0 flex-col leading-snug">
        <span className="font-medium wrap-anywhere">{p.name}</span>
        <span className="text-sm tabular-nums">
          {discount(p.discount_type, p.value, p.currency)}
        </span>
        <span className="text-muted text-sm tabular-nums">{window}</span>
        <span className="text-muted text-sm wrap-anywhere">
          {t("lists", { names: names.join(", ") })}
        </span>
      </p>
      <p className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm sm:flex-col sm:items-end">
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
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-haspopup="dialog"
          data-promotion={p.id}
          aria-label={t("openLabel", { name: p.name })}
          onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
          onClick={onOpen}
        >
          {t("open")}
        </Button>
      </p>
    </>
  );
}
