"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type branchesList, useBranchesUpdate } from "@/lib/api/client";
import type { Branch } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof branchesList>>>;
const verb = (active: boolean) => (active ? "activate" : "deactivate");

// Desactivar o reactivar una sucursal (F2-51), con `PATCH …/branches/{id}/` (F2-45). Hermana
// de «Suspender» en miembros: pide confirmación en la tarjeta y explica la respuesta. Quién
// puede hacerlo lo decide la API.
export function BranchStatusAction({
  slug,
  branch,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  branch: Branch;
  listKey: QueryKey;
  onAsk: () => void; // se abre una confirmación: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta tarjeta
}) {
  const t = useTranslations("branches.status");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Lo que se confirma (el estado que se pide) queda fijado al abrir: si la lista cambia
  // debajo, la pregunta y lo que se envía siguen siendo lo que el usuario leyó.
  const [target, setTarget] = useState<boolean | null>(null);
  const [done, setDone] = useState<boolean | null>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useBranchesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved, { branchId }) => {
        // Una lectura en vuelo traería el estado anterior a la escritura y pisaría la tarjeta:
        // se cancela. Si era la lista entera (no «Cargar más»), se vuelve a pedir después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (saved.id !== branchId) {
          return void queryClient.invalidateQueries({ queryKey: listKey });
        }
        // Solo el estado: los demás campos de la fila pudo cambiarlos «Editar» mientras tanto.
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === branchId ? { ...row, is_active: saved.is_active } : row,
                  ),
                })),
              }
            : data,
        );
        setDone(saved.is_active); // lo que respondió la API, no lo que se pidió
        setTarget(null);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        // La sucursal ya no existe: lo explica la lista, que se vuelve a pedir.
        if (error.status !== 404) return;
        const active = document.activeElement;
        // La tarjeta entera desaparece: también si el foco estaba en ella o en «Editar».
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        onStale(t("stale", { branch: branch.name }), here);
        setTarget(null);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401 && change.error.status !== 404;
  const busy = change.isPending || (change.isError && change.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta:
  // una pulsación entre las dos cosas reenviaría lo mismo. En un efecto de layout: uno pasivo
  // de un render anterior podría llegar después de la pulsación. Sin sesión (401) no se suelta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });
  // Otro lo hizo antes, con la confirmación abierta: no hay nada que confirmar.
  if (target !== null && target === branch.is_active && !busy) {
    setTarget(null);
    setDone(target);
  }

  const open = target !== null;
  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir, al cancelar y al terminar. El foco pasa al que lo
    // sustituye, salvo que el usuario ya esté en otra parte: al abrir, a «Cancelar», la opción
    // que no cambia nada.
    if (document.activeElement === document.body) {
      if (open) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function ask() {
    change.reset();
    setDone(null);
    setTarget(!branch.is_active);
    onAsk();
  }

  const offered = verb(!branch.is_active);
  return (
    <div
      ref={root}
      // Abierta ocupa su fila: «Editar», a su lado, pasa a otra línea.
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
    >
      {target !== null ? (
        <div
          role="group"
          aria-label={t(`${verb(target)}Label`, { branch: branch.name })}
          aria-describedby={question}
          className="flex flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t(`${verb(target)}Ask`, { branch: branch.name })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
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
                change.mutate({ orgSlug: slug, branchId: branch.id, data: { is_active: target } });
              }}
            >
              {t(`${verb(target)}${busy ? "Busy" : "Confirm"}`)}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t(`${offered}Label`, { branch: branch.name })}
          onClick={ask}
        >
          {t(offered)}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done !== null ? t(`${verb(done)}Done`, { branch: branch.name }) : ""}
      </p>
    </div>
  );
}
