"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { branchesList, getBranchesListQueryKey } from "@/lib/api/client";
import type { Branch } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { BranchCreate } from "./branch-create";
import { BranchEditAction } from "./branch-edit-action";

// Sucursales (F2-46): lo que devuelve `GET /api/v1/o/{slug}/branches/`, página a página. La
// lista, sus estados y su foco son los de `CursorList`. Quién puede verlas lo decide la API.
export function BranchesList() {
  const t = useTranslations();
  const { organization, permissions } = useTenant();
  // Comodidad: crear y editar se ofrecen a quien la API dijo que tiene `branches.manage`.
  const canManage = permissions.some((grant) => grant.code === "branches.manage");
  const listKey = [...getBranchesListQueryKey(organization.slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  // Una edición respondió que la pantalla ya no refleja a la API (F2-49). El aviso vive aquí:
  // el formulario, o la tarjeta entera, puede desaparecer cuando llega la lista nueva.
  const [notice, setNotice] = useState<string | null>(null);
  function stale(text: string, here: boolean) {
    setNotice(text);
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const ask = () => setNotice(null);

  return (
    <CursorList<Branch>
      ref={list}
      section="branches"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        branchesList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        <>
          {canManage ? (
            <BranchCreate slug={organization.slug} listKey={listKey} onAsk={ask} />
          ) : null}
          {notice ? (
            <p role="alert" className="text-danger">
              {notice}
            </p>
          ) : null}
        </>
      }
      empty={<p className="text-muted">{t("branches.empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] sm:items-start"
    >
      {(branch) => {
        // Calle, distrito y ciudad: lo que haya, en una línea.
        const place = [branch.address, branch.district, branch.city].filter((part) => part.trim());
        return (
          <>
            <p className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium wrap-anywhere">{branch.name}</span>
              <span className="text-muted text-sm wrap-anywhere">{branch.code}</span>
            </p>
            <div className="flex min-w-0 flex-col text-sm">
              {place.length > 0 ? (
                <p className="wrap-anywhere">{place.join(", ")}</p>
              ) : (
                <p className="text-muted">{t("branches.noAddress")}</p>
              )}
              {branch.phone.trim() ? (
                <p className="text-muted wrap-anywhere">
                  {t("branches.phone", { phone: branch.phone })}
                </p>
              ) : null}
              <p className="text-muted wrap-anywhere">
                {t("branches.timezone", { timezone: branch.timezone })}
              </p>
            </div>
            <p
              className={cn(
                "text-sm font-medium sm:text-right",
                branch.is_active ? "text-success" : "text-muted",
              )}
            >
              {t(branch.is_active ? "branches.active" : "branches.inactive")}
            </p>
            {canManage ? (
              <div className="flex flex-wrap items-start gap-2 sm:col-span-3">
                <BranchEditAction
                  slug={organization.slug}
                  branch={branch}
                  listKey={listKey}
                  onAsk={ask}
                  onStale={stale}
                />
              </div>
            ) : null}
          </>
        );
      }}
    </CursorList>
  );
}
