"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { Button } from "@/components/ui/button";
import {
  getMembersListQueryKey,
  getVariantPriceHistoryListQueryKey,
  membersList,
  variantPriceHistoryList,
} from "@/lib/api/client";
import type { ListPrice, PaginatedPriceList } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { stamp, ZONE } from "./zoned";

type Props = { slug: string; listId: string; listName: string; row: ListPrice };

// «Historial» de una variante en la lista elegida (F3-45c): `GET …/pricing/variants/{id}/
// price-history/?price_list_id=` (F3-44b), del más nuevo al más antiguo, por cursor. Se abre en
// un `<dialog>` modal: el navegador atrapa el foco, cierra con Escape y deja inerte la página;
// al cerrar, el foco vuelve al botón. Quién puede leerlo lo decide la API (`prices.manage`).
export function PriceHistory(props: Props) {
  const t = useTranslations("prices.history");
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const { variant } = props.row;
  const name = `${variant.product_name} ${variant.name} (${variant.sku})`;
  return (
    <>
      <Button
        ref={trigger}
        variant="ghost"
        className="border-border min-h-11 border sm:min-h-9"
        aria-haspopup="dialog"
        aria-label={t("openLabel", { name })}
        // Enter mantenido en «Cerrar» seguiría pulsando aquí y reabriría el panel.
        onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
        onClick={() => setOpen(true)}
      >
        {t("open")}
      </Button>
      {open ? (
        <Panel
          {...props}
          name={name}
          onClose={() => {
            setOpen(false);
            trigger.current?.focus();
          }}
        />
      ) : null}
    </>
  );
}

function Panel({
  slug,
  listId,
  listName,
  row,
  name,
  onClose,
}: Props & {
  name: string;
  onClose: () => void;
}) {
  const t = useTranslations("prices.history");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const zone = useTimeZone() ?? ZONE;
  const { permissions, user } = useTenant();
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const list = useRef<HTMLOListElement>(null);
  const moreButton = useRef<HTMLButtonElement>(null);
  const [arrived, setArrived] = useState<number | null>(null); // la primera fila nueva
  const id = useId();
  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    heading.current?.focus(); // el título primero: el lector de pantalla dice qué se abrió
  }, []);
  useEffect(() => {
    // Tras «Cargar más», como en `CursorList`: el foco pasa a lo que llegó (el botón puede
    // desaparecer), salvo que el usuario ya esté en otra parte.
    const active = document.activeElement;
    const still = [document.body, dialog.current, moreButton.current].includes(active as never);
    if (arrived !== null && still) (list.current?.children[arrived] as HTMLElement)?.focus();
  }, [arrived]);

  const params = { price_list_id: listId };
  const history = useInfiniteQuery<PaginatedPriceList, ApiError>({
    queryKey: [...getVariantPriceHistoryListQueryKey(slug, row.variant.id, params), "pages"],
    queryFn: ({ pageParam, signal }) =>
      variantPriceHistoryList(
        slug,
        row.variant.id,
        { ...params, ...(pageParam ? { cursor: pageParam as string } : {}) },
        { signal },
      ),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    gcTime: 0,
  });
  // El autor, como en las notas de clientes: el directorio solo con `users.view`.
  const people = useQuery({
    queryKey: [...getMembersListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((query) => membersList(slug, query, { signal })),
    enabled: permissions.some((grant) => grant.code === "users.view"),
    gcTime: 0,
  });
  function who(userId: string | null) {
    if (!userId) return t("system"); // no lo fijó una persona
    if (userId === user.id) return t("you");
    const one = people.data?.find((member) => member.user.id === userId)?.user;
    if (!one && people.isLoading) return "…";
    return one ? `${one.first_name} ${one.last_name}`.trim() || one.email : t("other");
  }
  const money = (amount: string, currency: string) =>
    format.number(Number(amount), { style: "currency", currency });
  async function loadMore() {
    const before = rows.length;
    const result = await history.fetchNextPage();
    const now = result.data?.pages.flatMap((page) => page.results).length ?? 0;
    if (now > before) setArrived(before);
  }

  const status = history.error?.status;
  const rows = history.data?.pages.flatMap((page) => page.results) ?? [];
  let body;
  if (history.data) {
    body = rows.length ? (
      <ol ref={list} aria-label={t("list")} className="divide-border flex flex-col divide-y">
        {rows.map((price) => (
          <li
            key={price.id}
            tabIndex={-1}
            className="flex flex-col gap-1 py-3 text-sm focus-visible:outline-none"
          >
            <p className="wrap-anywhere">
              <span className="font-medium tabular-nums">
                {stamp(price.created_at, zone).slice(0, 10)}
              </span>
              {` · ${who(price.created_by_user_id)} · `}
              <span className="tabular-nums">
                {price.previous_amount ? money(price.previous_amount, price.currency) : t("none")} →{" "}
                <span className="font-medium">{money(price.amount, price.currency)}</span>
              </span>
              {` · ${price.change_reason}`}
            </p>
            {/* Programado: rige en otro minuto (en la zona de la organización) que el que se fijó. */}
            {stamp(price.starts_at, zone) !== stamp(price.created_at, zone) ? (
              <p className="text-muted tabular-nums">
                {t("since", { at: stamp(price.starts_at, zone) })}
              </p>
            ) : null}
          </li>
        ))}
      </ol>
    ) : (
      <p className="text-muted">{t("empty")}</p>
    );
  } else if (status === 403 || status === 404) {
    body = <p role="alert">{t(status === 403 ? "denied" : "gone")}</p>;
  } else if (history.isError && status !== 401) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {errors(`api.${apiErrorKey(history.error)}`)}
        </p>
        <Button
          variant="ghost"
          className="border-border min-h-11 border"
          onClick={() => void history.refetch()}
        >
          {errors("retry")}
        </Button>
      </div>
    );
  } else {
    // Sin sesión (401), `Providers` ya lleva al login: queda el aviso de carga.
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <dialog
      ref={dialog}
      aria-labelledby={id}
      className="bg-surface text-foreground border-border fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-none w-full max-w-none overflow-y-auto border-l p-0 backdrop:bg-black/40 sm:max-w-md"
      onClose={onClose}
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
      onClick={(event) => {
        // Pulsar en el fondo cierra. El fondo también es el `<dialog>`: se mira dónde fue.
        const box = dialog.current!.getBoundingClientRect();
        const { clientX: x, clientY: y } = event;
        const inside = x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
        if (event.target === dialog.current && !inside) dialog.current.close();
      }}
    >
      <div className="flex min-h-full flex-col gap-4 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <h2
              id={id}
              ref={heading}
              tabIndex={-1}
              className="text-lg font-semibold focus-visible:outline-none"
            >
              {t("title")}
            </h2>
            <p className="text-muted text-sm wrap-anywhere">
              {t("subtitle", { name, list: listName })}
            </p>
          </div>
          <Button
            variant="ghost"
            className="size-11 shrink-0 text-xl"
            aria-label={t("close")}
            onClick={() => dialog.current?.close()}
          >
            <span aria-hidden>×</span>
          </Button>
        </div>
        {body}
        {history.hasNextPage ? (
          <Button
            ref={moreButton}
            variant="accent"
            size="lg"
            className="self-start"
            aria-disabled={history.isFetchingNextPage}
            onClick={() => history.isFetchingNextPage || void loadMore()}
          >
            {t(history.isFetchingNextPage ? "loadingMore" : "more")}
          </Button>
        ) : null}
        {history.isFetchNextPageError && status !== 401 ? (
          <p role="alert" className="text-danger text-sm">
            {errors(`api.${apiErrorKey(history.error)}`)}
          </p>
        ) : null}
      </div>
    </dialog>
  );
}
