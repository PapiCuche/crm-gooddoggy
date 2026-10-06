"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useTeamMembersRemove } from "@/lib/api/client";
import type { Team, TeamMember } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

const fullName = (user: TeamMember["user"]) => `${user.first_name} ${user.last_name}`.trim();

// Quitar a un integrante de un equipo (F2-66), con `DELETE …/teams/{id}/members/{membership_id}/`
// (F2-59). Vive en la fila del integrante, dentro del panel: pide confirmación ahí mismo y
// explica la respuesta. Quién puede hacerlo lo decide la API, que además no deja que nadie se
// quite a sí mismo.
export function TeamMemberRemove({
  slug,
  team,
  member,
  membersKey,
  onRemoved,
  onGone,
  focusClose,
}: {
  slug: string;
  team: Team;
  member: TeamMember;
  membersKey: QueryKey; // la lectura del panel: la fila sale de ella
  // Lo anuncia el panel: esta fila desaparece. `null` al enviar: el anuncio anterior se retira.
  onRemoved: (who: { id: string; name: string } | null) => void;
  onGone: (notice: string) => void; // 404: ya no estaba en el equipo, o el equipo no está al alcance
  focusClose: () => void;
}) {
  const t = useTranslations("teams.members.remove");
  const errors = useTranslations("errors");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [asking, setAsking] = useState(false);
  // Ya se quitó: desde ese render la fila no ofrece nada, salga o no del panel en él. Hoy sale
  // en el mismo (el anuncio repinta el panel); esta marca no depende de eso.
  const [removed, setRemoved] = useState(false);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  const name = fullName(member.user) || member.user.email;
  // Para saber a quién: dos personas pueden llamarse igual, y el correo las distingue.
  const exact = fullName(member.user) ? `${name} (${member.user.email})` : name;
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const remove = useTeamMembersRemove({
    mutation: {
      networkMode: "always",
      onSuccess: async () => {
        // El panel pudo cerrarse y reabrirse mientras se enviaba: su lectura en vuelo traería al
        // integrante de vuelta. Se cancela y, si no dejó datos donde escribir, se vuelve a pedir.
        const read = queryClient.getQueryState(membersKey);
        const rereading = !!read && read.fetchStatus !== "idle";
        await queryClient.cancelQueries({ queryKey: membersKey });
        queryClient.setQueryData<TeamMember[]>(membersKey, (rows) =>
          rows?.filter((row) => row.id !== member.id),
        );
        // La fila desaparece con sus botones: el foco, a «Cerrar», si seguía aquí o en ninguna parte.
        const active = document.activeElement;
        if (active === document.body || root.current?.contains(active)) focusClose();
        setRemoved(true);
        onRemoved({ id: member.id, name });
        if (rereading) void queryClient.invalidateQueries({ queryKey: membersKey });
      },
      onError: (error) => {
        if (error.status === 404) onGone(t("stale", { name, team: team.name }));
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = remove.isError && remove.error.status !== 401 && remove.error.status !== 404;
  const busy = remove.isPending || (remove.isError && remove.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta. En
  // un efecto de layout: uno pasivo de un render anterior podría llegar después de la pulsación.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cancelar: el foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte. Al abrir, a «Cancelar», que no cambia nada.
    if (document.activeElement === document.body) {
      if (asking) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = asking;
  }, [asking]);

  if (removed) return null;
  return (
    <div ref={root} className={cn("flex flex-col items-start gap-2", asking && "w-full")}>
      {asking ? (
        <div
          role="group"
          aria-label={t("label", { name: exact, team: team.name })}
          aria-describedby={question}
          className="flex flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { name, team: team.name })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {remove.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(`api.${apiErrorKey(remove.error)}`)}
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
                onRemoved(null);
                remove.mutate({ orgSlug: slug, teamId: team.id, membershipId: member.id });
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
          aria-label={t("label", { name: exact, team: team.name })}
          onClick={() => {
            remove.reset();
            setAsking(true);
          }}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
