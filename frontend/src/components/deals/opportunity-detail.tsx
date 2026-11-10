"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { notFound } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { moneyText } from "@/components/pricing/margins";
import { Button } from "@/components/ui/button";
import { getOpportunitiesRetrieveQueryKey, opportunitiesRetrieve } from "@/lib/api/client";
import type { Opportunity, OpportunityDetail as Detail } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { CloseControls } from "./close-opportunity";
import { MoveControl, MoveNotice, useMoveOpportunity } from "./move-opportunity";
import { daysInStage } from "./opportunity-card";
import { useOwners, usePipelines } from "./use-deals";

const BOX = "border-border bg-surface flex flex-col gap-3 rounded-lg border p-4";

// El detalle de una oportunidad (F5-07), con `GET /api/v1/o/{slug}/opportunities/{id}/` (F5-04c):
// cliente, importe, etapa, responsable, próxima acción, motivo de pérdida, líneas e historial de
// etapas; se cambia de etapa con las acciones del tablero (mover, ganar, perder, reabrir). Editar
// (`PATCH`, `assign`) llega en el siguiente corte (F5-07b). Fuera del alcance, borrada, de otra organización o inexistente la API
// responde el mismo 404 y la pantalla es la página de «no encontrada» de cualquier dirección. Qué
// puede hacer quien mira lo decide la API: aquí un 403 solo se explica.
export function OpportunityDetail({ opportunityId }: { opportunityId: string }) {
  const t = useTranslations("deals.detail");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const owners = useOwners();
  const pipelines = usePipelines(slug);
  const mover = useMoveOpportunity(slug);
  const key = getOpportunitiesRetrieveQueryKey(slug, opportunityId);
  const read = useQuery<Detail, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => opportunitiesRetrieve(slug, opportunityId, { signal }),
    gcTime: 0, // como la lista: al salir no queda en memoria
  });
  const [now] = useState(() => Date.now());
  const heading = useRef<HTMLHeadingElement>(null);
  const { refetch } = read;
  const waiting = useRef(0);
  useEffect(() => {
    // Una acción de etapa terminó (mover, ganar, perder, reabrir): lo mostrado se vuelve a leer.
    if (waiting.current > 0 && mover.pending.size === 0) void refetch();
    waiting.current = mover.pending.size;
  }, [mover.pending, refetch]);
  useEffect(() => {
    // El enlace pulsado ya no existe: el foco va al título, salvo que el usuario esté en otra parte.
    if (document.activeElement === document.body) heading.current?.focus();
  }, []);
  if (read.error?.status === 404) notFound();

  const detail = read.data;
  const problem = failed(read, t("denied"));
  let body: ReactNode;
  if (problem && !detail) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {problem}
        </p>
        {retry(read)}
      </div>
    );
  } else if (!detail) {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  } else {
    const pipeline = pipelines.data?.find(({ id }) => id === detail.pipeline_id);
    const targets = (pipeline?.stages ?? []).filter(
      (stage) => stage.is_active && stage.stage_type === "OPEN",
    );
    const card = detail as unknown as Opportunity; // los campos de la lista, que es lo que usan las acciones
    const when = (iso: string) => (
      <time dateTime={iso}>
        {format.dateTime(new Date(iso), { dateStyle: "medium", timeStyle: "short" })}
      </time>
    );
    const rows: [string, ReactNode][] = [
      [
        t("customer"),
        detail.contact.display_name === null ? (
          <span className="text-muted">{t("noContact")}</span>
        ) : (
          <Link
            href={`/o/${slug}/clientes/${detail.contact.id}`}
            prefetch={false}
            className="font-medium underline underline-offset-4"
          >
            {detail.contact.display_name}
          </Link>
        ),
      ],
      [t("amount"), moneyText(format, detail.amount, detail.currency)],
      [t("stage"), `${detail.stage.name} · ${t(`status.${detail.status}`)}`],
      [t("probability"), `${detail.probability} %`],
      [t("owner"), owners.name(detail.assigned_user_id)],
      [t("next"), detail.next_action ?? <span className="text-muted">{t("noNext")}</span>],
      [
        t("closeDate"),
        detail.expected_close_date ?? <span className="text-muted">{t("noCloseDate")}</span>,
      ],
      [t("days"), t("daysValue", { days: daysInStage(card, now) })],
      ...(detail.status === "LOST"
        ? ([
            [t("lostReason"), detail.lost_reason?.name ?? t("noReason")],
            ...(detail.lost_reason_note
              ? [[t("lostNote"), detail.lost_reason_note] as [string, ReactNode]]
              : []),
          ] as [string, ReactNode][])
        : []),
    ];
    body = (
      <>
        <dl className={`${BOX} sm:grid sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-x-6`}>
          {rows.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-muted text-sm">{label}</dt>
              <dd className="wrap-anywhere">{value}</dd>
            </div>
          ))}
        </dl>
        <section aria-label={t("actions")} className="flex flex-col gap-3">
          <MoveNotice said={mover.said} alertRef={mover.alertRef} statusRef={mover.statusRef} />
          <MoveControl card={card} targets={targets} mover={mover} />
          <CloseControls card={card} mover={mover} />
        </section>
        <section className={BOX} aria-label={t("items.title")}>
          <h2 className="font-medium">{t("items.title")}</h2>
          {detail.items.length === 0 ? (
            <p className="text-muted">{t("items.empty")}</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {detail.items.map((item, index) => (
                <li key={item.variant_id} className="flex flex-col gap-0.5 text-sm">
                  <span className="font-medium">
                    {t("items.line", {
                      ...{ index: index + 1, quantity: item.quantity },
                      price: moneyText(format, item.estimated_unit_price, detail.currency),
                    })}
                  </span>
                  {item.notes ? (
                    <span className="text-muted wrap-anywhere">{item.notes}</span>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
        <section className={BOX} aria-label={t("history.title")}>
          <h2 className="font-medium">{t("history.title")}</h2>
          <ol className="flex flex-col gap-2 text-sm">
            {detail.history.map((entry) => (
              <li key={entry.id} className="flex flex-col">
                <span className="wrap-anywhere">
                  {entry.from_stage
                    ? t("history.moved", { from: entry.from_stage.name, to: entry.to_stage.name })
                    : t("history.created", { to: entry.to_stage.name })}
                </span>
                <span className="text-muted">
                  {when(entry.changed_at)}
                  {entry.changed_by_user_id ? ` · ${owners.name(entry.changed_by_user_id)}` : ""}
                </span>
              </li>
            ))}
          </ol>
        </section>
      </>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <nav aria-label={t("breadcrumb")}>
          <ol className="flex flex-wrap gap-x-1.5 text-[13px] tracking-wide uppercase">
            <li>{t("eyebrow")}</li>
            <li>
              <span aria-hidden className="mr-1.5">
                /
              </span>
              <Link href={`/o/${slug}/oportunidades`} className="underline underline-offset-4">
                {t("list")}
              </Link>
            </li>
          </ol>
        </nav>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] wrap-anywhere focus-visible:outline-none"
        >
          {detail?.title ?? t("title")}
        </h1>
        {detail ? <p className="text-muted font-mono text-[13px]">{detail.number}</p> : null}
      </div>
      {body}
      {read.isError && detail && problem ? (
        <div className="flex flex-col items-start gap-2">
          <p role="alert" className="text-danger text-sm">
            {problem}
          </p>
          <Button variant="ghost" className="border-border border" onClick={() => void refetch()}>
            {errors("retry")}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
