"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { brandsList, categoriesList, productsList, variantsSearch } from "@/lib/api/client";

import type { KINDS } from "./promotion-targets";

export type Kind = (typeof KINDS)[number];
export type Target = { id: string; label: string };
// Cuántas coincidencias se piden: la lista que se ve es corta, el usuario afina el texto.
export const RESULTS = 8;
export const DEBOUNCE = 300;

// Busca en el catálogo por texto (`q`, F3-48i): nunca se baja el catálogo entero.
async function search(slug: string, kind: Kind, q: string, signal: AbortSignal): Promise<Target[]> {
  const [params, options] = [{ q, limit: RESULTS }, { signal }];
  if (kind === "variant_id") {
    const page = await variantsSearch(slug, params, options);
    return page.results.map((v) => ({
      id: v.id,
      label: `${v.product_name} · ${v.name} (${v.sku})`,
    }));
  }
  const list = { product_id: productsList, category_id: categoriesList, brand_id: brandsList }[
    kind
  ];
  return (await list(slug, params, options)).results.map((row) => ({
    id: row.id,
    label: row.name,
  }));
}

// El objetivo de una fila: un buscador con espera (`DEBOUNCE`) y, una vez elegido, su nombre
// con «Cambiar». La API no dice el estado de los resultados: los archivados también salen.
export function TargetPicker(props: {
  slug: string;
  kind: Kind;
  label: string;
  picked: Target | null;
  error?: string;
  onPick: (target: Target | null) => void;
}) {
  const { slug, kind, label, picked, error, onPick } = props;
  const t = useTranslations("promotions.form");
  const id = useId();
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setQ(typed.trim()), DEBOUNCE);
    return () => clearTimeout(timer);
  }, [typed]);
  const found = useQuery({
    queryKey: ["promotion-target-search", slug, kind, q],
    queryFn: ({ signal }) => search(slug, kind, q, signal),
    enabled: q.length >= 2 && !picked,
    retry: false,
  });
  const waiting = typed.trim() !== q || found.isFetching;
  let status = "";
  if (!picked && typed.trim().length >= 2) {
    if (waiting) status = t("searching");
    else if (found.isError) status = t("searchFailed");
    else status = t("results", { count: found.data?.length ?? 0 });
  }

  if (picked) {
    return (
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <p className="min-w-0 flex-1 text-sm font-medium wrap-anywhere">
            {label}: {picked.label}
          </p>
          <Button
            type="button"
            variant="ghost"
            className="border-border min-h-11 border sm:min-h-9"
            aria-label={t("change", { label })}
            onClick={() => {
              onPick(null);
              setTyped("");
            }}
          >
            {t("changeTarget")}
          </Button>
        </div>
        {error ? (
          <p role="alert" className="text-danger text-sm">
            {error}
          </p>
        ) : null}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={id}
        type="search"
        value={typed}
        autoComplete="off"
        maxLength={100}
        placeholder={t("searchPlaceholder")}
        aria-invalid={error ? true : undefined}
        aria-describedby={`${id}-status`}
        className="bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger h-11 rounded-[10px] border px-3 text-[16px]"
        onChange={(event) => setTyped(event.target.value)}
      />
      <p
        id={`${id}-status`}
        role={error ? "alert" : "status"}
        className={error ? "text-danger text-sm" : "text-muted text-sm"}
      >
        {error ?? status}
      </p>
      {!waiting && found.data?.length ? (
        <ul className="flex flex-col gap-1">
          {found.data.map((row) => (
            <li key={row.id}>
              <Button
                type="button"
                variant="ghost"
                className="border-border min-h-11 w-full justify-start border text-left whitespace-normal sm:min-h-9"
                onClick={() => onPick(row)}
              >
                {row.label}
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
