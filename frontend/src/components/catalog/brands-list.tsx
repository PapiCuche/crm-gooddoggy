"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { brandsList, getBrandsListQueryKey } from "@/lib/api/client";
import type { Brand } from "@/lib/api/model";

import { BrandCreate } from "./brand-create";
import { BrandDeleteAction } from "./brand-delete-action";
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
  // Una marca borrada sale de la lista: su resultado se anuncia aquí, que no desaparece.
  const [gone, setGone] = useState<string | null>(null);
  function deleted(text: string, here: boolean) {
    setGone(text);
    if (here) list.current?.focusHeading();
  }
  const ask = () => {
    setNotice(null);
    setGone(null);
  };

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
          {/* Siempre montado: un lector de pantalla anuncia el borrado cuando cambia. */}
          <p aria-live="polite" className={gone ? "text-sm wrap-anywhere" : "sr-only"}>
            {gone ?? ""}
          </p>
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
            // Abiertos, el formulario o la confirmación ocupan la fila: en la columna `auto`
            // estrecharían el nombre.
            <div className="flex flex-wrap items-start gap-2 sm:has-[form,[role=group]]:col-span-2">
              <BrandRenameAction
                slug={organization.slug}
                brand={brand}
                listKey={listKey}
                onAsk={ask}
                onStale={stale}
              />
              <BrandDeleteAction
                slug={organization.slug}
                brand={brand}
                listKey={listKey}
                onAsk={ask}
                onStale={stale}
                onDeleted={deleted}
              />
            </div>
          ) : null}
        </>
      )}
    </CursorList>
  );
}
