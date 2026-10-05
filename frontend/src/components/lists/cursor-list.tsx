"use client";

import { type QueryKey, useInfiniteQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, type Ref, useEffect, useImperativeHandle, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";

type Page<Row> = { results: Row[]; next: string | null };
export type CursorListHandle = { refetch: () => void; focusHeading: () => void };
// Los textos que esta lista lee de su sección. Un espacio de mensajes al que le falte alguno
// no es una sección válida: el fallo sale al compilar, no como una clave en pantalla.
type ListText =
  "eyebrow" | "title" | "intro" | "loading" | "denied" | "count" | "more" | "loadingMore";
type Section = {
  [Key in keyof typeof messages]: (typeof messages)[Key] extends Record<ListText, string>
    ? Key
    : never;
}[keyof typeof messages];

// Pantalla de un listado de tenant paginado por cursor (ADR-016): título, lista, «Cargar más» y
// los estados de carga, error, sin permiso y sin sesión. La comparten las pantallas de gestión
// (hoy, miembros). Quién puede ver la lista lo decide la API: aquí un 403 solo se explica.
// Los textos salen del espacio `section` de los mensajes.
export function CursorList<Row extends { id: string }>({
  section,
  organization,
  listKey,
  fetchPage,
  notice,
  rowClassName,
  children,
  ref,
}: {
  section: Section;
  organization: string;
  listKey: QueryKey;
  fetchPage: (cursor: string | undefined, signal: AbortSignal) => Promise<Page<Row>>;
  notice?: ReactNode; // aviso de la pantalla, encima de la lista
  rowClassName?: string;
  children: (row: Row) => ReactNode;
  ref?: Ref<CursorListHandle>;
}) {
  const t = useTranslations();
  const list = useInfiniteQuery<Page<Row>, ApiError>({
    queryKey: listKey,
    queryFn: ({ pageParam, signal }) =>
      fetchPage((pageParam as string | null) ?? undefined, signal),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    gcTime: 0, // como la guardia: al salir no queda en memoria y cada entrada pregunta a la API
  });
  const heading = useRef<HTMLHeadingElement>(null);
  const fresh = useRef<HTMLLIElement>(null); // la primera fila de la última página cargada
  const moreButton = useRef<HTMLButtonElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  const { refetch } = list;
  useImperativeHandle(
    ref,
    () => ({ refetch: () => void refetch(), focusHeading: () => heading.current?.focus() }),
    [refetch],
  );
  // El foco solo se mueve si sigue donde el usuario pulsó (o en ninguna parte).
  const still = (control: HTMLElement | null) =>
    document.activeElement === document.body || document.activeElement === control;
  const [retrying, setRetrying] = useState(false);
  // Como la guardia: una negativa cierra la lista aunque ya estuviera en pantalla, y sigue
  // cerrada hasta que la API responde bien (un fallo pasajero posterior no la reabre).
  const status = list.error?.status;
  const [denied, setDenied] = useState(false);
  if (status === 403 && !denied) setDenied(true);
  if (denied && list.isSuccess) setDenied(false);
  // «Cargar más»: ocupado desde la pulsación hasta la respuesta, también si espera a la red.
  const [more, setMore] = useState<"idle" | "busy" | "arrived">("idle");
  const pages = list.data?.pages ?? [];
  const rows = pages.flatMap((page) => page.results);
  const firstOfLast = pages.length > 1 ? pages.at(-1)?.results[0]?.id : undefined;

  useEffect(() => {
    // «Cargar más» puede desaparecer: el foco sigue en lo que llegó. Solo tras pulsarlo (no al
    // reabrir la pantalla con páginas en caché) y si el foco no se fue ya a otra parte.
    if (more === "arrived" && still(moreButton.current)) fresh.current?.focus();
  }, [more]);

  async function loadMore() {
    setMore("busy");
    const result = await list.fetchNextPage();
    // Llegó si hay una página más; un fallo o una petición cancelada no lo son.
    setMore((result.data?.pages.length ?? 0) > pages.length ? "arrived" : "idle");
    const closed = result.error?.status === 403; // la lista se cierra: el foco, al título
    if (closed && still(moreButton.current)) heading.current?.focus();
  }

  async function retry() {
    setRetrying(true);
    const result = await list.refetch();
    setRetrying(false);
    // La tarjeta se va (llegó la lista, o la API niega): el foco, al título.
    const gone = result.isSuccess || result.error?.status === 403;
    if (gone && still(retryButton.current)) heading.current?.focus();
  }

  let body;
  if (denied) {
    body = <p role="alert">{t(`${section}.denied`)}</p>;
  } else if (list.data) {
    body = (
      <>
        {notice}
        <ul aria-label={t(`${section}.title`)} className="flex flex-col gap-2">
          {rows.map((row) => (
            <li
              key={row.id}
              ref={row.id === firstOfLast ? fresh : undefined}
              tabIndex={row.id === firstOfLast ? -1 : undefined}
              className={cn(
                "border-border bg-surface rounded-lg border p-4 focus-visible:outline-none",
                rowClassName,
              )}
            >
              {children(row)}
            </li>
          ))}
        </ul>
        <p role="status" className="sr-only">
          {t(`${section}.count`, { count: rows.length })}
        </p>
        {list.hasNextPage ? (
          <div className="flex items-start gap-3">
            <Button
              ref={moreButton}
              variant="accent"
              size="lg"
              aria-disabled={more === "busy"}
              onClick={() => more === "busy" || void loadMore()}
            >
              {more === "busy" ? t(`${section}.loadingMore`) : t(`${section}.more`)}
            </Button>
            {/* Al lado del botón: no lo mueve de donde se pulsó ni queda fuera de la vista. Se
                quita al reintentar: si vuelve a fallar se monta de nuevo y se anuncia otra vez. */}
            {list.isFetchNextPageError && status !== 401 && more !== "busy" ? (
              <p role="alert" className="text-danger min-w-0 self-center text-sm">
                {t(`errors.api.${apiErrorKey(list.error)}`)}
              </p>
            ) : null}
          </div>
        ) : null}
      </>
    );
  } else if ((list.isError && status !== 401) || retrying) {
    // Sin sesión (401), `Providers` ya lleva al login: queda el aviso de carga, sin error.
    body = (
      <div className="flex flex-col items-start gap-3">
        {list.isError ? (
          <p role="alert" className="text-danger">
            {t(`errors.api.${apiErrorKey(list.error)}`)}
          </p>
        ) : null}
        <Button
          ref={retryButton}
          variant="primary"
          size="lg"
          aria-disabled={retrying}
          onClick={() => retrying || void retry()}
        >
          {retrying ? t(`${section}.loading`) : t("errors.retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t(`${section}.loading`)}
      </p>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t(`${section}.eyebrow`)}</p>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] focus-visible:outline-none"
        >
          {t(`${section}.title`)}
        </h1>
        <p className="text-muted">{t(`${section}.intro`, { organization })}</p>
      </div>
      {body}
    </section>
  );
}
