"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useTeamMembersPut } from "@/lib/api/client";
import type { Team, TeamMember } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";

type Role = TeamMember["team_role"];
// Lo contrario de cada papel que esta versión conoce. Con otro, no se ofrece nada.
const OTHER: Partial<Record<string, Role>> = { MEMBER: "SUPERVISOR", SUPERVISOR: "MEMBER" };
const fullName = (user: TeamMember["user"]) => `${user.first_name} ${user.last_name}`.trim();

// Cambiar el papel de un integrante en su equipo (F2-67), con
// `PUT …/teams/{id}/members/{membership_id}/` (F2-58). Vive en la fila del integrante, dentro
// del panel. Sin confirmación: el mismo botón lo deshace. Quién puede hacerlo lo decide la API,
// que además no deja que nadie cambie su propia pertenencia.
export function TeamMemberRole({
  slug,
  team,
  member,
  membersKey,
  onGone,
}: {
  slug: string;
  team: Team;
  member: TeamMember;
  membersKey: QueryKey; // la lectura del panel: la fila enseña el papel desde ella
  onGone: (notice: string) => void; // 404: el equipo o la persona ya no están al alcance
}) {
  const t = useTranslations("teams.members.setRole");
  const errors = useTranslations("errors");
  const queryClient = useQueryClient();
  // Lo que respondió la última escritura, y el papel que la fila tenía al enviarla. Vale
  // mientras el panel siga con ese papel: así el botón pasa a la acción contraria en el mismo
  // render que deja pulsar otra vez, sin esperar a que el panel se entere.
  const [saved, setSaved] = useState<{ from: string; to: Role } | null>(null);
  const [done, setDone] = useState<Role | null>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos pulsaciones seguidas no deben ser dos peticiones.
  const sending = useRef(false);
  const name = fullName(member.user) || member.user.email;
  // Para saber a quién: dos personas pueden llamarse igual, y el correo las distingue.
  const exact = fullName(member.user) ? `${name} (${member.user.email})` : name;
  const role = saved?.from === member.team_role ? saved.to : member.team_role;
  const target = OTHER[role];
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const change = useTeamMembersPut({
    mutation: {
      networkMode: "always",
      onSuccess: async (answer) => {
        // El panel pudo cerrarse y reabrirse mientras se enviaba: su lectura en vuelo traería el
        // papel de antes. Se cancela y, si no dejó datos donde escribir, se vuelve a pedir.
        const read = queryClient.getQueryState(membersKey);
        const rereading = !!read && read.fetchStatus !== "idle";
        await queryClient.cancelQueries({ queryKey: membersKey });
        // La fila, como la respondió la API: su papel y lo demás, que es lo más reciente que hay.
        queryClient.setQueryData<TeamMember[]>(membersKey, (rows) =>
          rows?.map((row) => (row.id === member.id ? answer : row)),
        );
        setSaved({ from: member.team_role, to: answer.team_role });
        setDone(answer.team_role);
        if (rereading) void queryClient.invalidateQueries({ queryKey: membersKey });
      },
      onError: (error) => {
        if (error.status === 404) onGone(t("stale", { name, team: team.name }));
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = change.isError && change.error.status !== 401 && change.error.status !== 404;
  const busy = change.isPending || (change.isError && change.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta. En
  // un efecto de layout: uno pasivo de un render anterior podría llegar después de la pulsación.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  if (!target) return null;
  return (
    <div className="flex flex-col items-start gap-1">
      {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
      <Button
        variant="ghost"
        className="border-border min-h-11 border sm:min-h-9"
        aria-label={t(`to${target}Label`, { name: exact, team: team.name })}
        aria-disabled={busy}
        // Al terminar, este botón pasa a la acción contraria: el segundo clic de un doble clic
        // no debe deshacer lo recién hecho.
        onClick={(event) => {
          if (event.detail > 1 || sending.current) return;
          sending.current = true;
          setDone(null);
          change.mutate({
            orgSlug: slug,
            teamId: team.id,
            membershipId: member.id,
            data: { team_role: target },
          });
        }}
      >
        {t(busy ? "busy" : `to${target}`)}
      </Button>
      {failed ? (
        <p role="alert" className="text-danger text-sm">
          {change.error.code === "PERMISSION_DENIED"
            ? t("denied")
            : errors(
                // No hay campos que revisar: una validación fallida es un fallo nuestro.
                `api.${change.error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(change.error)}`,
              )}
        </p>
      ) : null}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. La fila ya
          enseña el papel a quien ve la pantalla. */}
      <p role="status" className="sr-only">
        {done && Object.hasOwn(OTHER, done) ? t(`done${done}`, { name, team: team.name }) : ""}
      </p>
    </div>
  );
}
