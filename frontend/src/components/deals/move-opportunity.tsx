"use client";

import { type InfiniteData, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { getOpportunitiesListQueryKey, opportunitiesMove } from "@/lib/api/client";
import type {
  Opportunity,
  OpportunityDetail,
  PaginatedOpportunityList,
  Stage,
} from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

type Said = { text: string; alert?: boolean } | null;
export type Mover = ReturnType<typeof useMoveOpportunity>;

// Qué dijo la API, en el mensaje que le toca; `null`: uno genérico de `errors.api`. Un 403 es
// `opportunities.update` (la API decide, no el nombre del rol).
function reason(error: unknown) {
  if (!(error instanceof ApiError)) return null;
  if (error.status === 409) {
    return error.code === "STALE_VERSION"
      ? "stale"
      : error.code === "OPPORTUNITY_CLOSED"
        ? "closed"
        : "conflict";
  }
  return ({ 400: "invalid", 403: "denied", 404: "gone" } as const)[error.status] ?? null;
}

// De la respuesta (el detalle), solo los campos de la lista: sin líneas ni historial en la caché.
const fresh = (card: Opportunity, detail: OpportunityDetail) =>
  Object.fromEntries(
    Object.keys(card).map((key) => [key, detail[key as keyof Opportunity]]),
  ) as unknown as Opportunity;

// Mover una oportunidad a otra etapa (F5-05b): `POST …/opportunities/{id}/move/` con la `version`
// leída. Antes de tocar la caché se cancela toda lectura de listas en curso (si no, una respuesta
// vieja pisaría el cambio). La tarjeta pasa a la columna nueva al instante, en todas las listas
// de la organización en caché (los filtros pueden cambiar mientras tanto); con 200 se sustituye
// por lo que devuelve la API (versión nueva), y con un rechazo vuelve a su sitio con el motivo.
// Tras un rechazo (salvo 403: nada cambió) las listas se piden otra vez, pero cuando ya no queda
// otra orden en curso, para no deshacer su cambio. Una petición por oportunidad a la vez. El
// arrastre (#528) reutiliza `move`.
export function useMoveOpportunity(slug: string) {
  const t = useTranslations("deals.move");
  const errors = useTranslations("errors.api");
  const client = useQueryClient();
  const lists = { queryKey: getOpportunitiesListQueryKey(slug) };
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [said, setSaid] = useState<Said>(null);
  const sending = useRef(new Set<string>());
  const dirty = useRef(false); // hay que releer las listas cuando acabe la última orden
  const focusRef = useRef<string | null>(null); // la tarjeta cambia de columna: el foco la sigue
  const alertRef = useRef<HTMLParagraphElement>(null);

  const put = (next: Opportunity) =>
    client.setQueriesData<InfiniteData<PaginatedOpportunityList, string>>(lists, (old) =>
      old?.pages
        ? {
            ...old,
            pages: old.pages.map((page) => ({
              ...page,
              results: page.results.map((row) => (row.id === next.id ? next : row)),
            })),
          }
        : old,
    );

  async function move(card: Opportunity, to: Stage) {
    if (sending.current.has(card.id) || card.stage.id === to.id) return;
    sending.current.add(card.id);
    setPending(new Set(sending.current));
    setSaid(null);
    try {
      if (client.isFetching(lists)) dirty.current = true; // lo cancelado se relee al terminar
      await client.cancelQueries(lists);
      focusRef.current = card.id;
      put({ ...card, stage: { id: to.id, name: to.name, type: to.stage_type } });
      put(
        fresh(
          card,
          await opportunitiesMove(slug, card.id, { stage_id: to.id, version: card.version }),
        ),
      );
      setSaid({ text: t("done", { title: card.title, stage: to.name }) });
    } catch (error) {
      put(card);
      focusRef.current = card.id;
      const why = reason(error);
      const text = why ? t(why) : errors(apiErrorKey(error));
      setSaid({ text: t("back", { reason: text }), alert: true });
      if (why !== "denied") dirty.current = true;
    } finally {
      sending.current.delete(card.id);
      setPending(new Set(sending.current));
    }
    if (sending.current.size > 0 || !dirty.current) return;
    dirty.current = false;
    await client.invalidateQueries(lists);
    // La tarjeta pudo irse con la lista nueva (404, o cerrada): el foco, al aviso, no a la nada.
    setTimeout(() => {
      focusRef.current = null;
      if (document.activeElement === document.body) alertRef.current?.focus();
    }, 0);
  }
  return { move, pending, said, focusRef, alertRef, sending };
}

// El aviso de la última acción: el éxito se anuncia en una región de estado (montada desde el
// principio, para que un lector de pantalla lo lea al cambiar) y el rechazo, como alerta visible.
export function MoveNotice({ said, alertRef }: { said: Said; alertRef: Mover["alertRef"] }) {
  return (
    <>
      <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p
          ref={alertRef}
          tabIndex={-1}
          role="alert"
          className="text-danger text-sm wrap-anywhere focus-visible:outline-none"
        >
          {said.text}
        </p>
      ) : null}
    </>
  );
}

// «Mover a…» en una tarjeta abierta: las etapas abiertas y activas del pipeline menos la actual
// (ganar y perder son otras acciones). Elegir y pulsar «Mover» son dos pasos: con el teclado,
// recorrer las opciones no mueve nada. Se ofrece a todos: si falta permiso, la API responde 403.
export function MoveControl({
  card,
  targets,
  mover,
}: {
  card: Opportunity;
  targets: readonly Stage[];
  mover: Mover;
}) {
  const t = useTranslations("deals.move");
  const hint = useId();
  const [target, setTarget] = useState("");
  const [asked, setAsked] = useState(false); // pulsó «Mover» sin elegir etapa
  const select = useRef<HTMLSelectElement>(null);
  const { focusRef } = mover;
  useEffect(() => {
    if (focusRef.current !== card.id) return;
    focusRef.current = null;
    // Solo si el foco se quedó sin sitio (la tarjeta se movió): no se le quita a quien ya se fue a otra parte.
    if (document.activeElement === document.body) select.current?.focus();
  }, [card.id, focusRef]);
  const options = targets.filter(({ id }) => id !== card.stage.id);
  const busy = mover.pending.has(card.id);
  const chosen = options.find(({ id }) => id === target);
  if (card.status !== "OPEN" || options.length === 0) return null;
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex gap-2">
        <select
          ref={select}
          value={target}
          aria-label={t("label", { title: card.title })}
          aria-disabled={busy}
          aria-describedby={asked && !chosen ? hint : undefined}
          className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 min-w-0 flex-1 rounded-[10px] border px-2 text-[16px]"
          onChange={(event) => busy || setTarget(event.target.value)}
        >
          <option value="">{t("placeholder")}</option>
          {options.map(({ id, name }) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
        <Button
          variant="primary"
          className="min-h-11 aria-disabled:cursor-not-allowed aria-disabled:opacity-50 sm:min-h-9"
          aria-label={busy ? undefined : t("buttonLabel", { title: card.title })}
          aria-disabled={busy || !chosen}
          onClick={() => busy || (chosen ? void mover.move(card, chosen) : setAsked(true))}
        >
          {busy ? t("moving") : t("button")}
        </Button>
      </div>
      {asked && !chosen ? (
        <p id={hint} role="alert" className="text-danger text-sm">
          {t("choose")}
        </p>
      ) : null}
    </div>
  );
}
