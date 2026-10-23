"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { getInventoryImportsRowsListQueryKey, inventoryImportsRowsList } from "@/lib/api/client";
import type { ImportRowActionEnum, StockImport, StockImportRow } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Page = { results: StockImportRow[]; next: string | null };
type Action = Extract<ImportRowActionEnum, "CREATE" | "UPDATE" | "UNCHANGED" | "ERROR" | "STALE">;
const REVIEW: Action[] = ["CREATE", "UPDATE", "UNCHANGED", "ERROR"];
const RESULT: Action[] = [...REVIEW, "STALE"]; // STALE solo existe tras aplicar

// Las filas de una importación de stock (F3-22b), en el orden del archivo, 50 por página con
// «Cargar más» (`GET …/inventory/imports/{id}/rows/`). El filtro es el de la API: pide solo las
// de esa acción. Aplicar cambia las filas (STALE, ERROR, aplicadas): el estado de la importación
// va en la clave y la lista se vuelve a pedir; revertir (F3-24), igual: las que volvieron al
// saldo de antes se marcan como revertidas, y las que no, STALE o ERROR con su motivo. Un 403 cierra la pantalla (`onDenied`); un 401
// lo trata `Providers`.
export function StockImportRows({
  slug,
  job,
  onDenied,
}: {
  slug: string;
  job: StockImport;
  onDenied: () => void;
}) {
  const t = useTranslations("stockImport");
  const errors = useTranslations("errors");
  const id = useId();
  const applied = job.status === "APPLIED" || job.status === "REVERTED";
  const done = job.status === "REVERTED" ? "rows.reverted" : "rows.applied";
  const [action, setAction] = useState<Action | "">("");
  const filter = action ? { action } : {};
  const rows = useInfiniteQuery<Page, ApiError>({
    queryKey: [...getInventoryImportsRowsListQueryKey(slug, job.id, filter), job.status],
    queryFn: ({ pageParam, signal }) =>
      inventoryImportsRowsList(
        slug,
        job.id,
        { ...filter, ...(pageParam ? { cursor: pageParam as string } : {}) },
        { signal },
      ),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    gcTime: 0,
  });
  const status = rows.error?.status;
  useEffect(() => {
    if (status === 403) onDenied();
  }, [status, onDenied]);
  const pages = rows.data?.pages ?? [];
  const list = pages.flatMap((page) => page.results);
  // El foco y los estados ocupados, como en `CursorList`: «Cargar más» y «Reintentar» quedan
  // ocupados desde la pulsación hasta la respuesta, también si la petición espera a la red.
  const heading = useRef<HTMLHeadingElement>(null);
  const fresh = useRef<HTMLLIElement>(null); // la primera fila de la última página cargada
  const moreButton = useRef<HTMLButtonElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  const firstOfLast = pages.length > 1 ? pages.at(-1)?.results[0]?.id : undefined;
  const [more, setMore] = useState<"idle" | "busy" | "arrived">("idle");
  const [retrying, setRetrying] = useState(false);
  const shown = useRef(0); // cada filtro es otra lista: lo que llega de la anterior no cuenta
  // El foco solo se mueve si sigue donde el usuario pulsó (o en ninguna parte).
  const still = (control: HTMLElement | null) =>
    document.activeElement === document.body || document.activeElement === control;
  useEffect(() => {
    // «Cargar más» puede desaparecer (última página): el foco sigue en lo que llegó.
    if (more === "arrived" && still(moreButton.current)) fresh.current?.focus();
  }, [more]);
  async function loadMore() {
    const list = shown.current;
    setMore("busy");
    const result = await rows.fetchNextPage();
    if (list !== shown.current) return;
    setMore((result.data?.pages.length ?? 0) > pages.length ? "arrived" : "idle");
  }
  async function retry() {
    const list = shown.current;
    setRetrying(true);
    const result = await rows.refetch();
    if (list !== shown.current) return;
    setRetrying(false);
    // Al abrir la lista, el foco va al título (la tarjeta de error se va).
    if (result.isSuccess && still(retryButton.current)) heading.current?.focus();
  }

  let body;
  if (rows.data) {
    body = list.length ? (
      <ul aria-labelledby={`${id}-title`} className="flex flex-col gap-2">
        {list.map((row) => {
          const moved = applied && (row.action === "CREATE" || row.action === "UPDATE");
          const wrong = row.action === "ERROR" || row.action === "STALE";
          const { quantity, on_hand_seen: seen } = row; // sin los dos (fila con error), lo que vino
          return (
            <li
              key={row.id}
              ref={row.id === firstOfLast ? fresh : undefined}
              tabIndex={row.id === firstOfLast ? -1 : undefined}
              className="border-border bg-surface grid gap-x-4 gap-y-1 rounded-lg border p-3 text-sm focus-visible:outline-none sm:grid-cols-[5.5rem_minmax(0,1fr)_auto] sm:items-start"
            >
              <span className="text-muted tabular-nums">
                {t("rows.line", { line: row.row_number })}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="font-medium wrap-anywhere">
                  {t("rows.place", {
                    sku: row.sku || t("rows.missing"),
                    almacen: row.almacen || t("rows.missing"),
                  })}
                </span>
                <span className="text-muted wrap-anywhere tabular-nums">
                  {quantity !== null && seen !== null
                    ? t("rows.seen", { seen, quantity })
                    : t("rows.raw", { value: row.cantidad })}
                </span>
                {row.errors.map((error, index) => (
                  <span key={index} className="text-danger wrap-anywhere">
                    {error}
                  </span>
                ))}
              </span>
              <span
                className={cn(
                  "font-medium sm:text-right",
                  moved && job.status === "APPLIED" && "text-success",
                  wrong && "text-danger",
                )}
              >
                {moved
                  ? t(done, { action: t(`rows.action.${row.action}`) })
                  : t(`rows.action.${row.action}`)}
              </span>
            </li>
          );
        })}
      </ul>
    ) : (
      <p className="text-muted">{t("rows.empty")}</p>
    );
  } else if ((rows.isError && status !== 401 && status !== 403) || retrying) {
    // Sin sesión (401) `Providers` lleva al login y un 403 cierra la pantalla: sin tarjeta.
    body = (
      <div className="flex flex-col items-start gap-3">
        {rows.isError ? (
          <p role="alert" className="text-danger">
            {errors(`api.${apiErrorKey(rows.error)}`)}
          </p>
        ) : null}
        <Button
          ref={retryButton}
          variant="primary"
          size="lg"
          aria-disabled={retrying}
          onClick={() => retrying || void retry()}
        >
          {retrying ? t("rows.loading") : errors("retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("rows.loading")}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h3
          ref={heading}
          id={`${id}-title`}
          tabIndex={-1}
          className="text-lg font-semibold focus-visible:outline-none"
        >
          {t("rows.title")}
        </h3>
        <div className="flex w-full flex-col gap-1.5 sm:w-56">
          <label htmlFor={`${id}-action`} className="text-sm font-medium">
            {t("rows.show")}
          </label>
          {/* 16 px en el control: por debajo, Safari en iOS amplía la página al enfocarlo. */}
          <select
            id={`${id}-action`}
            value={action}
            className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border px-3 text-[16px]"
            onChange={(event) => {
              shown.current += 1;
              setMore("idle");
              setRetrying(false);
              setAction(event.target.value as Action | "");
            }}
          >
            <option value="">{t("rows.all")}</option>
            {(applied ? RESULT : REVIEW).map((value) => (
              <option key={value} value={value}>
                {t(`count.${value}`)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {body}
      <p role="status" className="sr-only">
        {rows.data ? t("rows.count", { count: list.length }) : ""}
      </p>
      {rows.data && rows.hasNextPage ? (
        <div className="flex items-start gap-3">
          <Button
            ref={moreButton}
            variant="accent"
            size="lg"
            aria-disabled={more === "busy"}
            onClick={() => more === "busy" || void loadMore()}
          >
            {more === "busy" ? t("rows.loadingMore") : t("rows.more")}
          </Button>
          {rows.isFetchNextPageError && status !== 401 && more !== "busy" ? (
            <p role="alert" className="text-danger min-w-0 self-center text-sm">
              {errors(`api.${apiErrorKey(rows.error)}`)}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
