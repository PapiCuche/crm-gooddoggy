"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { brandsList, getBrandsListQueryKey } from "@/lib/api/client";
import type { Brand } from "@/lib/api/model";

import { BrandCreate } from "./brand-create";
import { BrandRenameAction } from "./brand-rename-action";

// Marcas del catálogo (F3-02): lo que devuelve `GET /api/v1/o/{slug}/brands/`, página a página
// y por orden de creación. La lista, sus estados y su foco son los de `CursorList`. Quién puede
// verlas o cambiarlas lo decide la API (`products.view`, `brands.manage`).
export function BrandsList() {
  const t = useTranslations();
  const { organization, permissions } = useTenant();
  // Comodidad: las escrituras se ofrecen a quien la API dijo que tiene `brands.manage`.
  const canManage = permissions.some((grant) => grant.code === "brands.manage");
  const listKey = [...getBrandsListQueryKey(organization.slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  const [notice, setNotice] = useState<string | null>(null);
  function stale(text: string, here: boolean) {
    setNotice(text);
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const ask = () => setNotice(null);

  return (
    <CursorList<Brand>
      ref={list}
      section="brands"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        brandsList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        <>
          {canManage ? (
            <BrandCreate slug={organization.slug} listKey={listKey} onAsk={ask} />
          ) : null}
          {notice ? (
            <p role="alert" className="text-danger">
              {notice}
            </p>
          ) : null}
        </>
      }
      empty={<p className="text-muted">{t("brands.empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(brand) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{brand.name}</span>
            <span className="text-muted font-mono text-[13px] wrap-anywhere">{brand.slug}</span>
          </p>
          {canManage ? (
            // Abierto, el formulario ocupa la fila entera: en la columna `auto` estrecharía el nombre.
            <div className="flex flex-wrap items-start gap-2 sm:has-[form]:col-span-2">
              <BrandRenameAction
                slug={organization.slug}
                brand={brand}
                listKey={listKey}
                onAsk={ask}
                onStale={stale}
              />
            </div>
          ) : null}
        </>
      )}
    </CursorList>
  );
}
