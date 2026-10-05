"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type membersList, useMembersSetStatus } from "@/lib/api/client";
import type { Member } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";

type Pages = InfiniteData<Awaited<ReturnType<typeof membersList>>>;
type Target = "SUSPENDED" | "ACTIVE";
const VERB = { SUSPENDED: "suspend", ACTIVE: "reactivate" } as const;
// El estado que la pantalla cree ya no es el de la API: lo explica la lista, que se vuelve a pedir.
const STALE = new Set(["INVALID_TRANSITION", "NOT_FOUND"]);

// Suspender o reactivar a un miembro (F2-21). Quién puede hacerlo lo decide la API (ADR-017):
// la pantalla solo ofrece la acción, pide confirmación y explica la respuesta.
export function MemberStatusAction({
  slug,
  member,
  name,
  organization,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  member: Member;
  name: string;
  organization: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre una confirmación: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("members.action");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Lo que se confirma queda fijado al abrir: si la lista cambia debajo, la pregunta y lo que
  // se envía siguen siendo lo que el usuario leyó.
  const [target, setTarget] = useState<Target | null>(null);
  const [done, setDone] = useState<Target | null>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useMembersSetStatus({
    mutation: {
      networkMode: "always",
      onSuccess: async (result, { membershipId }) => {
        // Una lectura en vuelo traería el estado anterior a la escritura y pisaría la fila: se
        // cancela. Si era la lista entera (no «Cargar más»), se vuelve a pedir después: quien
        // la pidió, otra fila con la pantalla desfasada, sigue necesitándola.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (result.id !== membershipId) {
          return void queryClient.invalidateQueries({ queryKey: listKey });
        }
        // La fila cambia en la lista ya cargada: no se piden otra vez todas sus páginas.
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === membershipId ? { ...row, status: result.status } : row,
                  ),
                })),
              }
            : data,
        );
        setDone(result.status); // lo que respondió la API, no lo que se pidió
        setTarget(null);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        if (!STALE.has(error.code)) return;
        const active = document.activeElement;
        onStale(t("stale", { name }), active === document.body || !!root.current?.contains(active));
        setTarget(null);
      },
      // Sin sesión sigue ocupado hasta que cambia la página.
      onSettled: (_result, error) => void (sending.current = error?.status === 401),
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401 && !STALE.has(change.error.code);
  const busy = change.isPending || (change.isError && change.error.status === 401);
  // Otro lo hizo antes, con la confirmación abierta: no hay nada que confirmar.
  if (target && target === member.status && !change.isPending) {
    setTarget(null);
    setDone(target);
  }

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir, al cancelar y al terminar. El foco pasa al que lo
    // sustituye, salvo que el usuario ya esté en otra parte: al abrir, a «Cancelar», la opción
    // que no cambia nada.
    if (document.activeElement === document.body) {
      if (target) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = !!target;
  }, [target]);

  function open() {
    change.reset();
    setDone(null);
    setTarget(member.status === "ACTIVE" ? "SUSPENDED" : "ACTIVE");
    onAsk();
  }

  const offered = VERB[member.status === "ACTIVE" ? "SUSPENDED" : "ACTIVE"];
  return (
    <div ref={root} className="flex flex-col items-start gap-2 sm:col-span-3 sm:items-end">
      {target ? (
        <div
          role="group"
          aria-label={t(`${VERB[target]}Label`, { name })}
          aria-describedby={question}
          className="flex flex-col items-start gap-2 sm:items-end"
        >
          <p id={question} className="text-sm sm:text-right">
            {t(`${VERB[target]}Ask`, { name, organization })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm sm:text-right">
              {change.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    change.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR" // no hay campos que revisar: es un fallo nuestro
                      : apiErrorKey(change.error),
                  )}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setTarget(null)}
            >
              {t("cancel")}
            </Button>
            {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => {
                if (sending.current) return;
                sending.current = true;
                change.mutate({ orgSlug: slug, membershipId: member.id, data: { status: target } });
              }}
            >
              {t(`${VERB[target]}${busy ? "Busy" : "Confirm"}`)}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t(`${offered}Label`, { name })}
          onClick={open}
        >
          {t(offered)}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done ? t(`${VERB[done]}Done`, { name }) : ""}
      </p>
    </div>
  );
}
