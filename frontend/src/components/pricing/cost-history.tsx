"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { shouldRetry } from "@/app/providers";
import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import {
  costHistoryList,
  getCostHistoryListQueryKey,
  getMembersListQueryKey,
  membersList,
} from "@/lib/api/client";
import type { CostPage, PriceVariant } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { focusAfterPaint } from "./cost-form";
import { moneyText } from "./margins";
import { stamp, ZONE } from "./zoned";

type Props = { slug: string; variant: PriceVariant };

// «Historial de costos» de una variante (F3-47, `product_cost.manage`): `GET …/pricing/variants/
// {id}/costs/`, del más nuevo al más antiguo, por cursor, en un panel lateral `<dialog>` modal
// como el historial de precios. La ruta pide un segundo factor reciente: `withStepUp` abre el
// diálogo del código y repite la lectura. Solo se pintan campos explícitos de cada costo. Al
// cerrar (o al apagar «Ver costos y margen») el panel se desmonta y su caché se borra.
export function CostHistory(props: Props) {
  const t = useTranslations("prices.costs.history");
  const { withStepUp, dialog: stepUp } = useStepUp();
  const [open, setOpen] = useState(false);
  const { variant } = props;
  const name = `${variant.product_name} ${variant.name} (${variant.sku})`;
  return (
    <>
      <Button
        variant="ghost"
        className="border-border min-h-11 border sm:min-h-9"
        aria-haspopup="dialog"
        data-cost-history={variant.id}
        aria-label={t("openLabel", { name })}
        onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
        onClick={() => setOpen(true)}
      >
        {t("open")}
      </Button>
      {open ? (
        <Panel
          {...props}
          name={name}
          withStepUp={withStepUp}
          onClose={() => {
            setOpen(false);
            focusAfterPaint(`[data-cost-history="${variant.id}"]`);
          }}
        />
      ) : null}
      {stepUp}
    </>
  );
}

function Panel({
  slug,
  variant,
  name,
  withStepUp,
  onClose,
}: Props & {
  name: string;
  withStepUp: ReturnType<typeof useStepUp>["withStepUp"];
  onClose: () => void;
}) {
  const t = useTranslations("prices.costs.history");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const zone = useTimeZone() ?? ZONE;
  const { permissions, user } = useTenant();
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const list = useRef<HTMLOListElement>(null);
  const [arrived, setArrived] = useState<number | null>(null); // la primera fila nueva
  const id = useId();
  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    heading.current?.focus(); // el título primero: el lector de pantalla dice qué se abrió
  }, []);
  useEffect(() => {
    // Tras «Cargar más» el foco pasa a lo que llegó, salvo que ya esté en otra parte.
    const active = document.activeElement;
    const free = active === document.body || active === dialog.current;
    if (arrived !== null && free) (list.current?.children[arrived] as HTMLElement)?.focus();
  }, [arrived]);

  const history = useInfiniteQuery<CostPage, ApiError>({
    queryKey: [...getCostHistoryListQueryKey(slug, variant.id), "pages"],
    queryFn: ({ pageParam, signal }) =>
      withStepUp(() =>
        costHistoryList(slug, variant.id, pageParam ? { cursor: pageParam as string } : {}, {
          signal,
        }),
      ),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    // Una cancelación no se repite sola: volvería a abrir el diálogo del código.
    retry: (failures, error) => !isStepUpCancelled(error) && shouldRetry(failures, error),
    gcTime: 0,
  });
  const people = useQuery({
    queryKey: [...getMembersListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((query) => membersList(slug, query, { signal })),
    enabled: permissions.some((grant) => grant.code === "users.view"),
    gcTime: 0,
  });
  function who(userId: string | null) {
    if (!userId) return t("system");
    if (userId === user.id) return t("you");
    const one = people.data?.find((member) => member.user.id === userId)?.user;
    return one ? `${one.first_name} ${one.last_name}`.trim() || one.email : t("other");
  }
  const money = (amount: string, currency: string) => moneyText(format, amount, currency);
  async function loadMore() {
    const before = rows.length;
    const result = await history.fetchNextPage();
    const now = result.data?.pages.flatMap((page) => page.results).length ?? 0;
    if (now > before) setArrived(before);
  }

  const status = history.error?.status;
  const rows = history.data?.pages.flatMap((page) => page.results) ?? [];
  const current = history.data?.pages[0]?.current;
  let body;
  if (history.data) {
    body = (
      <>
        <p className="text-sm font-medium tabular-nums">
          {current
            ? t("current", { amount: money(current.amount, current.currency) })
            : t("noCurrent")}
        </p>
        {rows.length ? (
          <ol ref={list} aria-label={t("list")} className="divide-border flex flex-col divide-y">
            {rows.map((cost) => (
              <li
                key={cost.id}
                tabIndex={-1}
                className="flex flex-col gap-1 py-3 text-sm focus-visible:outline-none"
              >
                <p className="wrap-anywhere">
                  <span className="font-medium tabular-nums">
                    {money(cost.amount, cost.currency)}
                  </span>
                  {` · ${stamp(cost.starts_at, zone)} · ${who(cost.created_by_user_id)}`}
                  {` · ${t(`source.${cost.source}`)} · ${cost.change_reason}`}
                </p>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-muted">{t("empty")}</p>
        )}
      </>
    );
  } else if (isStepUpCancelled(history.error)) {
    body = <p role="status">{t("declined")}</p>;
  } else if (status === 403 || status === 404) {
    body = <p role="alert">{t(status === 403 ? "denied" : "gone")}</p>;
  } else if (history.isError && status !== 401) {
    body = (
      <p role="alert" className="text-danger">
        {errors(`api.${apiErrorKey(history.error)}`)}
      </p>
    );
  } else {
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
            <p className="text-muted text-sm wrap-anywhere">{name}</p>
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
        {history.isError && !history.data && ![401, 403, 404].includes(status ?? 0) ? (
          <Button
            variant="ghost"
            className="border-border min-h-11 w-fit border"
            onClick={() => void history.refetch()}
          >
            {errors("retry")}
          </Button>
        ) : null}
        {history.hasNextPage ? (
          <Button
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
