"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { FieldsForm } from "@/components/forms/fields-form";
import { useMembers } from "@/components/inbox/members";
import { Button } from "@/components/ui/button";
import {
  getInventoryLevelsListQueryKey,
  getInventoryMovementsListQueryKey,
  getInventoryReservationsListQueryKey,
  inventoryReservationsList,
  useInventoryReservationsRelease,
} from "@/lib/api/client";
import type { Level, Reservation, Variant, Warehouse } from "@/lib/api/model";

import { useReads } from "./cell-picker";

// Tras reservar o liberar (F3-36): el historial se marca para volver a pedirse y se espera a las
// reservas y a los saldos, para anunciar lo reservado y lo disponible de la celda tal como queda.
// `null` si la celda ya no está en pantalla (el usuario eligió otra variante mientras tanto).
export async function refetchCell(
  client: QueryClient,
  slug: string,
  { variant_id, warehouse_id }: Pick<Reservation, "variant_id" | "warehouse_id">,
) {
  void client.invalidateQueries({ queryKey: getInventoryMovementsListQueryKey(slug) });
  await Promise.all(
    [getInventoryReservationsListQueryKey(slug), getInventoryLevelsListQueryKey(slug)].map(
      (queryKey) => client.invalidateQueries({ queryKey }),
    ),
  );
  const key = [...getInventoryLevelsListQueryKey(slug, { variant_id }), "all"];
  const rows = client.getQueryData<Level[]>(key);
  return rows ? (rows.find((row) => row.warehouse_id === warehouse_id) ?? ZERO) : null;
}
const ZERO = { reserved: 0, available: 0 }; // una celda sin movimientos
export const SHORT = { dateStyle: "medium", timeStyle: "short" } as const;

// Las reservas activas de la celda elegida (`GET …/inventory/reservations/?status=ACTIVE`,
// F3-35b), por vencimiento: la API solo ordena por creación (ADR-016). Una ACTIVE con
// `expires_at` ya pasado venció y el job aún no la cerró (hasta 5 minutos): se dice, y no se
// ofrece «Liberar» (sería 409). Con `inventory.manage`, «Liberar» pide un motivo y confirma.
export function CellReservations({
  slug,
  warehouse,
  variant,
  canManage,
  onDone,
}: {
  slug: string;
  warehouse: Warehouse;
  variant: Variant;
  canManage: boolean;
  onDone: (text: string | null) => void;
}) {
  const t = useTranslations("reservations");
  const format = useFormatter();
  const queryClient = useQueryClient();
  const { user } = useTenant();
  const { name } = useMembers();
  const { failed, retry } = useReads();
  const [releasing, setReleasing] = useState<Reservation | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const params = { variant_id: variant.id, warehouse_id: warehouse.id, status: "ACTIVE" as const };
  const list = useQuery({
    queryKey: [...getInventoryReservationsListQueryKey(slug, params), "all"],
    queryFn: ({ signal }) =>
      everyPage((page) => inventoryReservationsList(slug, { ...page, ...params }, { signal })),
    gcTime: 0,
  });
  const when = (at: string) => format.dateTime(new Date(at), SHORT);
  const units = (quantity: number) => t("units", { quantity });
  const sent = useRef(""); // el anuncio, con la celda tal como era al liberar
  const release = useInventoryReservationsRelease({
    mutation: {
      networkMode: "always", // sin red falla y se dice; no queda en cola
      onSuccess: async (ended) => {
        const cell = await refetchCell(queryClient, slug, ended); // sigue «Liberando…»
        setReleasing(null);
        onDone([sent.current, cell && t("figures", cell)].filter(Boolean).join(" "));
      },
      onError: (error) => {
        // Venció, se consumió o ya no existe: la fila ya no es lo que se ve. Se cierra y se dice.
        if (error.code !== "RESERVATION_NOT_ACTIVE" && error.status !== 404) return;
        setReleasing(null);
        onDone(t(error.status === 404 ? "missing" : "notActive"));
        void refetchCell(queryClient, slug, params);
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco va al título de la lista (la fila pudo irse), salvo que esté en otra parte.
    if (!releasing && opened.current && document.activeElement === document.body) {
      heading.current?.focus();
    }
    opened.current = !!releasing;
  }, [releasing]);
  function show(next: Reservation | null) {
    release.reset();
    if (next) onDone(null);
    setReleasing(next);
  }

  let body = <p className="text-muted">{t("none")}</p>;
  if (list.isPending) body = <p className="text-muted">{t("loading")}</p>;
  if (list.isError)
    body = (
      <>
        <p role="alert" className="text-danger">
          {failed(list, t("denied"))}
        </p>
        {retry(list)}
      </>
    );
  if (list.data?.length) {
    const now = list.dataUpdatedAt; // vencida: según el reloj de cuando llegó la lista
    const rows = [...list.data].sort((a, b) => a.expires_at.localeCompare(b.expires_at));
    body = (
      <ul aria-label={t("title")} className="flex flex-col gap-2">
        {rows.map((row) => {
          const lapsed = Date.parse(row.expires_at) <= now;
          const by = row.created_by_user_id;
          const who = by === user.id ? t("you") : (by && name(by)) || t("other");
          return (
            <li key={row.id} className="border-border bg-surface rounded-lg border p-4">
              {releasing?.id === row.id ? (
                <FieldsForm
                  title={t("releaseTitle")}
                  heading={
                    <h3 className="font-medium wrap-anywhere">
                      {t("releaseHeading", { units: units(row.quantity), reason: row.reason })}
                    </h3>
                  }
                  className="flex flex-col gap-4"
                  fields={[
                    {
                      name: "reason",
                      max: 255,
                      label: t("releaseReason"),
                      required: t("reasonRequired"),
                      invalid: t("reasonInvalid"),
                      hint: t("releaseHint"),
                    },
                  ]}
                  write={release}
                  send={({ reason }) => {
                    const where = { variant: variant.name, warehouse: warehouse.name };
                    sent.current = t("released", { units: units(row.quantity), ...where });
                    release.mutate({ orgSlug: slug, reservationId: row.id, data: { reason } });
                  }}
                  denied={t("releaseDenied")}
                  labels={{ submit: t("release"), busy: t("releaseBusy"), cancel: t("cancel") }}
                  onCancel={() => show(null)}
                />
              ) : (
                <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
                  <p className="flex min-w-0 flex-col leading-snug">
                    <span className="font-medium tabular-nums">{units(row.quantity)}</span>
                    <span className="wrap-anywhere">{row.reason}</span>
                    <span className={lapsed ? "text-danger text-sm" : "text-muted text-sm"}>
                      {t(lapsed ? "lapsed" : "until", { at: when(row.expires_at) })}
                    </span>
                    <span className="text-muted text-sm wrap-anywhere">{t("by", { who })}</span>
                  </p>
                  {canManage && !lapsed ? (
                    <Button
                      variant="ghost"
                      className="border-border min-h-11 justify-self-start border sm:min-h-9"
                      onClick={() => show(row)}
                    >
                      {t("release")}
                      <span className="sr-only">{`: ${units(row.quantity)}, ${row.reason}`}</span>
                    </Button>
                  ) : null}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    );
  }
  return (
    <section className="flex flex-col gap-2">
      <h2 ref={heading} tabIndex={-1} className="font-medium focus-visible:outline-none">
        {t("title")}
      </h2>
      {body}
    </section>
  );
}
