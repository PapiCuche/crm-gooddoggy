"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { opportunitiesMove } from "@/lib/api/client";
import type { Opportunity, PaginatedOpportunityList, Stage } from "@/lib/api/model";
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

// Mover una oportunidad a otra etapa (F5-05b): `POST …/opportunities/{id}/move/` con la `version`
// leída. La tarjeta pasa a la columna nueva al instante (en la lista en caché); si la API
// responde, se sustituye por el detalle que devuelve (con la versión nueva), y si la rechaza
// vuelve a su sitio con el motivo y la lista se pide otra vez (salvo con un 403: nada cambió).
// Una petición por oportunidad a la vez. El arrastre (#528) reutiliza `move`.
export function useMoveOpportunity(slug: string, listKey: QueryKey) {
  const t = useTranslations("deals.move");
  const errors = useTranslations("errors.api");
  const client = useQueryClient();
  const [pending, setPending] = useState<ReadonlySet<string>>(new Set());
  const [said, setSaid] = useState<Said>(null);
  const sending = useRef(new Set<string>());
  const focusRef = useRef<string | null>(null); // la tarjeta cambia de columna: el foco la sigue

  const put = (next: Opportunity) =>
    client.setQueryData<InfiniteData<PaginatedOpportunityList, string>>(
      listKey,
      (old) =>
        old && {
          ...old,
          pages: old.pages.map((page) => ({
            ...page,
            results: page.results.map((row) => (row.id === next.id ? next : row)),
          })),
        },
    );

  async function move(card: Opportunity, to: Stage) {
    if (sending.current.has(card.id) || card.stage.id === to.id) return;
    sending.current.add(card.id);
    setPending(new Set(sending.current));
    setSaid(null);
    focusRef.current = card.id;
    put({ ...card, stage: { id: to.id, name: to.name, type: to.stage_type } });
    try {
      put(await opportunitiesMove(slug, card.id, { stage_id: to.id, version: card.version }));
      setSaid({ text: t("done", { title: card.title, stage: to.name }) });
    } catch (error) {
      put(card);
      focusRef.current = card.id;
      const why = reason(error);
      const text = why ? t(why) : errors(apiErrorKey(error));
      setSaid({ text: t("back", { reason: text }), alert: true });
      if (why !== "denied") void client.invalidateQueries({ queryKey: listKey });
    } finally {
      sending.current.delete(card.id);
      setPending(new Set(sending.current));
    }
  }
  return { move, pending, said, focusRef };
}

// El aviso de la última acción: el éxito se anuncia en una región de estado (montada desde el
// principio, para que un lector de pantalla lo lea al cambiar) y el rechazo, como alerta visible.
export function MoveNotice({ said }: { said: Said }) {
  return (
    <>
      <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm wrap-anywhere">
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
  const [target, setTarget] = useState("");
  const select = useRef<HTMLSelectElement>(null);
  const { focusRef } = mover;
  useEffect(() => {
    if (focusRef.current !== card.id) return;
    focusRef.current = null;
    select.current?.focus();
  }, [card.id, focusRef]);
  const options = targets.filter(({ id }) => id !== card.stage.id);
  const busy = mover.pending.has(card.id);
  const chosen = options.find(({ id }) => id === target);
  if (card.status !== "OPEN" || options.length === 0) return null;
  return (
    <div className="flex gap-2">
      <select
        ref={select}
        value={target}
        aria-label={t("label", { title: card.title })}
        aria-disabled={busy}
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
        className="min-h-11 sm:min-h-9"
        aria-disabled={busy || !chosen}
        onClick={() => busy || !chosen || void mover.move(card, chosen)}
      >
        {busy ? t("moving") : t("button")}
      </Button>
    </div>
  );
}
