"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type membersList, useMembersSetStatus } from "@/lib/api/client";
import type { Member } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";

type Pages = InfiniteData<Awaited<ReturnType<typeof membersList>>>;
// El estado que la pantalla cree ya no es el de la API: la lista se vuelve a pedir.
const STALE = new Set(["INVALID_TRANSITION", "NOT_FOUND"]);

// Suspender o reactivar a un miembro (F2-21). Quién puede hacerlo lo decide la API (ADR-017):
// la pantalla solo ofrece la acción, pide confirmación y explica la respuesta.
export function MemberStatusAction({
  slug,
  member,
  name,
  organization,
  listKey,
}: {
  slug: string;
  member: Member;
  name: string;
  organization: string;
  listKey: QueryKey;
}) {
  const t = useTranslations("members.action");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const suspending = member.status === "ACTIVE";
  const verb = suspending ? "suspend" : "reactivate";
  const [asking, setAsking] = useState(false);
  const [done, setDone] = useState<"suspend" | "reactivate" | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useMembersSetStatus({
    mutation: {
      networkMode: "always",
      onSuccess: (result) => {
        // La fila cambia en la lista ya cargada: no se piden otra vez todas sus páginas.
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === result.id ? { ...row, status: result.status } : row,
                  ),
                })),
              }
            : data,
        );
        setDone(verb);
        setAsking(false);
      },
      onError: (error) => {
        if (STALE.has(error.code)) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      // Sin sesión sigue ocupado hasta que cambia la página.
      onSettled: (_result, error) => void (sending.current = error?.status === 401),
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401;
  const busy = change.isPending || (change.isError && !failed);

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir, al cancelar y al terminar. El foco pasa al que lo
    // sustituye, salvo que el usuario ya esté en otra parte: al abrir, a «Cancelar», la opción
    // que no cambia nada.
    if (document.activeElement === document.body) {
      if (asking) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = asking;
  }, [asking]);

  function open() {
    change.reset();
    setDone(null);
    setAsking(true);
  }

  return (
    <div className="flex flex-col items-start gap-2 sm:col-span-3 sm:items-end">
      {asking ? (
        <div
          role="group"
          aria-label={t(`${verb}Label`, { name })}
          className="flex flex-col items-start gap-2 sm:items-end"
        >
          <p className="text-sm sm:text-right">{t(`${verb}Ask`, { name, organization })}</p>
          {failed ? (
            <p role="alert" className="text-danger text-sm sm:text-right">
              {change.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(apiErrorKey(change.error))}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setAsking(false)}
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
                change.mutate({
                  orgSlug: slug,
                  membershipId: member.id,
                  data: { status: suspending ? "SUSPENDED" : "ACTIVE" },
                });
              }}
            >
              {busy ? t(`${verb}Busy`) : t(`${verb}Confirm`)}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t(`${verb}Label`, { name })}
          onClick={open}
        >
          {t(verb)}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done ? t(`${done}Done`, { name }) : ""}
      </p>
    </div>
  );
}
