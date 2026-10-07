"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type invitationsList, useInvitationsSetStatus } from "@/lib/api/client";
import type { Invitation } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";

type Pages = InfiniteData<Awaited<ReturnType<typeof invitationsList>>>;

// Revocar una invitación pendiente o caducada (F2-85), con `PUT …/invitations/{id}/status/`
// (F2-82). Hermana de «Desactivar» en sucursales: pide confirmación en la fila y explica la
// respuesta. Aquí la acción desaparece de la fila al terminar: el resultado lo dice la lista.
// Quién puede revocar cada invitación lo decide la API.
export function InvitationRevokeAction({
  slug,
  invitation,
  listKey,
  onAsk,
  onSettle,
  onLost,
}: {
  slug: string;
  invitation: Invitation;
  listKey: QueryKey;
  onAsk: () => void; // se abre una confirmación: el aviso anterior de la lista ya no aplica
  // Terminó: lo que la lista debe decir, si hay que volver a pedirla (`stale`) y si el foco
  // seguía en esta fila (`here`), cuyos controles desaparecen.
  onSettle: (text: string, stale: boolean, here: boolean) => void;
  onLost: () => void; // los controles desaparecieron con el foco en ellos
}) {
  const t = useTranslations("invitations.revoke");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  const { email } = invitation;
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const revoke = useInvitationsSetStatus({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved, { invitationId }) => {
        // Una lectura en vuelo traería el estado anterior a la escritura y pisaría la fila: se
        // cancela. Si era la lista entera (no «Cargar más»), se vuelve a pedir después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (saved.id !== invitationId) {
          return void queryClient.invalidateQueries({ queryKey: listKey });
        }
        const focused = here(); // antes de que la fila cambie y sus controles desaparezcan
        // Solo el estado, y el que respondió la API: lo demás de la fila no cambia al revocar.
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === invitationId ? { ...row, status: saved.status } : row,
                  ),
                })),
              }
            : data,
        );
        onSettle(t("done", { email }), false, focused);
        setOpen(false);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        // Ya no está al alcance (404) o ya no estaba pendiente (409): lo explica la lista, que
        // se vuelve a pedir.
        const gone = error.status === 404;
        if (!gone && error.code !== "INVALID_TRANSITION") return;
        onSettle(t(gone ? "stale" : "settled", { email }), true, here());
        setOpen(false);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const settled = revoke.error?.status === 404 || revoke.error?.code === "INVALID_TRANSITION";
  const failed = revoke.isError && revoke.error.status !== 401 && !settled;
  const busy = revoke.isPending || (revoke.isError && revoke.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta:
  // una pulsación entre las dos cosas reenviaría lo mismo. En un efecto de layout: uno pasivo
  // de un render anterior podría llegar después de la pulsación. Sin sesión (401) no se suelta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });
  // Solo se revoca lo que la lista enseña como pendiente o caducado.
  const offered = invitation.status === "PENDING" || invitation.status === "EXPIRED";
  // Otro la revocó, o se aceptó, con la confirmación abierta: no hay nada que confirmar.
  if (open && !offered && !busy) setOpen(false);

  const was = useRef({ open: false, offered });
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cancelar. El foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte: al abrir, a «Cancelar», la opción que no
    // cambia nada. Si desaparecen todos (la fila dejó de poder revocarse), lo recoge la lista.
    if (document.activeElement === document.body) {
      if (!offered) {
        if (was.current.offered) onLost();
      } else if (open) cancel.current?.focus();
      else if (was.current.open) trigger.current?.focus();
    }
    was.current = { open, offered };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `onLost` es de cada render
  }, [open, offered]);

  function ask() {
    revoke.reset();
    setOpen(true);
    onAsk();
  }

  if (!offered) return null;
  return (
    <div
      ref={root}
      className="flex flex-col items-start gap-2 sm:col-span-3"
      // Enter mantenido repite la pulsación: reabriría la confirmación o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <div
          role="group"
          aria-label={t("label", { email })}
          aria-describedby={question}
          className="flex flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { email })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {revoke.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    revoke.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR" // no hay campos que revisar: es un fallo nuestro
                      : apiErrorKey(revoke.error),
                  )}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setOpen(false)}
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
                revoke.mutate({
                  orgSlug: slug,
                  invitationId: invitation.id,
                  data: { status: "REVOKED" },
                });
              }}
            >
              {t(busy ? "busy" : "confirm")}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("label", { email })}
          onClick={ask}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
