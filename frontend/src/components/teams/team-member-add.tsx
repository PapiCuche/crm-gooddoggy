"use client";

import { type QueryKey, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { getMembersListQueryKey, membersList, useTeamMembersPut } from "@/lib/api/client";
import type { Member, Team, TeamMember } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

// Todos los miembros de la organización: los candidatos se enseñan juntos, así que sigue el cursor.
async function allPeople(slug: string, signal: AbortSignal): Promise<Member[]> {
  const found: Member[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await membersList(slug, { limit: 200, ...(cursor ? { cursor } : {}) }, { signal });
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}

const fullName = (user: Member["user"]) => `${user.first_name} ${user.last_name}`.trim();
const display = (user: Member["user"]) => fullName(user) || user.email;
// Para elegir a quién: dos personas pueden llamarse igual, y el correo las distingue.
const exact = (user: Member["user"]) =>
  fullName(user) ? `${fullName(user)} (${user.email})` : user.email;

// La lista de candidatos. Solo existe mientras está abierta: al cerrarla, su lectura se cancela
// y se olvida, y la siguiente apertura vuelve a preguntar.
function Candidates({
  slug,
  team,
  present,
  out,
  membersKey,
  onGone,
  onDone,
}: {
  slug: string;
  team: Team;
  present: ReadonlySet<string>; // las membresías que ya están en el equipo
  out: { id: string; name: string } | null; // a quién se acaba de quitar en el panel (F2-66)
  membersKey: QueryKey; // la lectura del panel: el integrante nuevo entra en ella
  onGone: (notice: string) => void; // 404: el equipo o la persona ya no están al alcance
  onDone: () => void;
}) {
  const t = useTranslations("teams.members.add");
  const errors = useTranslations("errors");
  const { membership_id: own } = useTenant();
  const queryClient = useQueryClient();
  const done = useRef<HTMLButtonElement>(null);
  const [added, setAdded] = useState<{ id: string; name: string } | null>(null);
  // Quien ya entró desde esta lista: su botón se va en el mismo render que suelta la marca; el
  // panel, de donde sale `present`, se entera una tarea después.
  const [joined, setJoined] = useState<readonly string[]>([]);
  // Quien entró desde esta lista y después se quitó en el panel vuelve a ser candidato: su fila
  // recupera el botón, y el anuncio de que entró se retira.
  const [seen, setSeen] = useState(out);
  if (out !== seen) {
    setSeen(out);
    // Por lo que enseña el panel y no solo por `out`: dos respuestas en un mismo render dejan una.
    const stay = joined.filter((id) => present.has(id));
    if (out && stay.length < joined.length) {
      setJoined(stay);
      if (added && !stay.includes(added.id)) setAdded(null); // por `id`: hay tocayos
    }
  }
  // Una escritura cada vez, y enviada una sola vez (el estado de la mutación llega a la
  // pantalla una tarea después de la pulsación).
  const sending = useRef(false);
  const people = useQuery<Member[], ApiError>({
    queryKey: [...getMembersListQueryKey(slug), "all"],
    queryFn: ({ signal }) => allPeople(slug, signal),
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false, // no por su cuenta: un botón se desmontaría con el foco
    refetchOnReconnect: false,
  });
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const add = useTeamMembersPut({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved, { membershipId }) => {
        // El panel pudo cerrarse y reabrirse mientras se enviaba: su lectura en vuelo traería la
        // lista de antes. Se cancela y, como no deja datos donde escribir, se vuelve a pedir.
        const read = queryClient.getQueryState(membersKey);
        const rereading = !!read && read.fetchStatus !== "idle";
        await queryClient.cancelQueries({ queryKey: membersKey });
        // El integrante nuevo, con lo que respondió la API, al final: es el último en entrar.
        queryClient.setQueryData<TeamMember[]>(membersKey, (rows) =>
          rows ? [...rows.filter((row) => row.id !== saved.id), saved] : rows,
        );
        setJoined((ids) => [...ids, membershipId]); // la fila que se pulsó
        setAdded({ id: membershipId, name: display(saved.user) });
        if (rereading) void queryClient.invalidateQueries({ queryKey: membersKey });
      },
      onError: (error, { membershipId }) => {
        if (error.status !== 404) return;
        const who = people.data?.find((person) => person.id === membershipId);
        onGone(t("stale", { name: who ? display(who.user) : "", team: team.name }));
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = add.isError && add.error.status !== 401 && add.error.status !== 404;
  const busy = add.isPending || (add.isError && add.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta:
  // una pulsación entre las dos cosas reenviaría lo mismo. Sin sesión (401) no se suelta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  // Quien ya está, quien tiene la membresía dada de baja y uno mismo no se ofrecen: la API no
  // deja que nadie se incorpore a sí mismo. Quien entra desde esta lista conserva su fila, sin
  // botón, hasta cerrarla o hasta que se le quite en el panel: así la persona siguiente no sube
  // bajo el puntero.
  const candidates = people.data?.filter(
    (person) =>
      joined.includes(person.id) ||
      (!present.has(person.id) && person.id !== own && person.status !== "DEACTIVATED"),
  );
  const left = candidates?.filter((person) => !joined.includes(person.id)).length;
  useEffect(() => {
    // El botón del recién incorporado desaparece: el foco, a «Listo», si se quedó en ninguna
    // parte. También al abrir.
    if (document.activeElement === document.body) done.current?.focus();
  }, [left]);

  const error = people.error && people.error.status !== 401 ? people.error : null;
  return (
    <div
      role="group"
      aria-label={t("title", { team: team.name })}
      className="border-border flex w-full flex-col gap-2 border-t pt-2"
    >
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium wrap-anywhere">{t("title", { team: team.name })}</p>
        <Button
          ref={done}
          variant="ghost"
          className="min-h-11 shrink-0 sm:min-h-9"
          aria-disabled={busy}
          onClick={() => sending.current || onDone()}
        >
          {t("done")}
        </Button>
      </div>
      {candidates ? (
        <>
          <ul aria-label={t("title", { team: team.name })} className="flex flex-col gap-1">
            {candidates.map((person) => {
              const mine = busy && add.variables?.membershipId === person.id;
              return (
                <li
                  key={person.id} // la altura del botón, también cuando ya no lo tiene
                  className="flex min-h-11 items-center justify-between gap-3 sm:min-h-9"
                >
                  <span className="flex min-w-0 flex-col text-sm leading-snug wrap-anywhere">
                    {display(person.user)}
                    {fullName(person.user) ? (
                      <span className="text-muted">{person.user.email}</span>
                    ) : null}
                  </span>
                  {joined.includes(person.id) ? (
                    <span className="text-muted shrink-0 text-sm">{t("joined")}</span>
                  ) : (
                    <Button
                      variant="ghost"
                      className="border-border min-h-11 shrink-0 border sm:min-h-9"
                      aria-label={t("addLabel", { name: exact(person.user), team: team.name })}
                      aria-disabled={busy}
                      aria-busy={mine}
                      // `aria-disabled` y no `disabled`: conserva el foco mientras se envía. El
                      // segundo clic de un doble clic no debe contar como otra pulsación.
                      onClick={(event) => {
                        if (event.detail > 1 || sending.current) return;
                        sending.current = true;
                        setAdded(null);
                        add.mutate({
                          orgSlug: slug,
                          teamId: team.id,
                          membershipId: person.id,
                          data: {},
                        });
                      }}
                    >
                      {t(mine ? "addBusy" : "add")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
          {left === 0 ? <p className="text-muted text-sm">{t("none")}</p> : null}
          {present.has(own) ? null : <p className="text-muted text-sm">{t("selfNote")}</p>}
        </>
      ) : error ? (
        <div className="flex flex-col items-start gap-2">
          <p role="alert" className="text-danger text-sm">
            {errors(`api.${apiErrorKey(error)}`)}
          </p>
          <Button
            variant="primary"
            className="min-h-11 sm:min-h-9"
            // Al volver a pedir, este botón desaparece: el foco pasa antes a «Listo», que se queda.
            onClick={() => {
              done.current?.focus();
              void people.refetch();
            }}
          >
            {errors("retry")}
          </Button>
        </div>
      ) : (
        <p role="status" className="text-muted text-sm">
          {t("loading")}
        </p>
      )}
      {failed ? (
        <p role="alert" className="text-danger text-sm">
          {add.error.code === "PERMISSION_DENIED"
            ? t("denied")
            : errors(
                // No hay campos que revisar: una validación fallida es un fallo nuestro.
                `api.${add.error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(add.error)}`,
              )}
        </p>
      ) : null}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={added ? "text-sm wrap-anywhere" : "sr-only"}>
        {added ? t("added", { name: added.name, team: team.name }) : ""}
      </p>
    </div>
  );
}

// Incorporar a un miembro de la organización a un equipo (F2-65), con
// `PUT …/teams/{id}/members/{membership_id}/` (F2-58). Vive dentro del panel de integrantes.
// Quién puede hacerlo lo decide la API, que además no deja que nadie se incorpore a sí mismo.
export function TeamMemberAdd({
  slug,
  team,
  present,
  out,
  membersKey,
  onGone,
}: {
  slug: string;
  team: Team;
  present: ReadonlySet<string>;
  out: { id: string; name: string } | null;
  membersKey: QueryKey;
  onGone: (notice: string) => void;
}) {
  const t = useTranslations("teams.members.add");
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, «Listo» desaparece: el foco vuelve a «Incorporar integrante», salvo que el
    // usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  return open ? (
    <Candidates
      slug={slug}
      team={team}
      present={present}
      out={out}
      membersKey={membersKey}
      onGone={onGone}
      onDone={() => setOpen(false)}
    />
  ) : (
    <Button
      ref={trigger}
      variant="ghost"
      className="border-border min-h-11 self-start border sm:min-h-9"
      aria-label={t("openLabel", { team: team.name, slug: team.slug })}
      onClick={() => setOpen(true)}
    >
      {t("open")}
    </Button>
  );
}
