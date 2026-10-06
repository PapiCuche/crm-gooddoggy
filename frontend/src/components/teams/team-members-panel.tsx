"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { getTeamMembersListQueryKey, teamMembersList } from "@/lib/api/client";
import type { Team, TeamMember } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";
import { TeamMemberAdd } from "./team-member-add";
import { TeamMemberRemove } from "./team-member-remove";

// Los papeles y los estados con nombre propio. Uno que esta versión no conozca se enseña con
// su código: solo cuentan las claves propias del catálogo.
const ROLES: Record<string, string> = messages.teams.members.role;
const STATUSES: Record<string, string> = messages.members.status;
const named = (names: Record<string, string>, code: string) =>
  Object.hasOwn(names, code) ? (names[code] ?? code) : code;

// Todos los integrantes del equipo: el panel los enseña juntos, así que sigue el cursor.
async function allMembers(slug: string, teamId: string, signal: AbortSignal) {
  const found: TeamMember[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const params = { limit: 200, ...(cursor ? { cursor } : {}) };
    const page = await teamMembersList(slug, teamId, params, { signal });
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}

const fullName = (user: TeamMember["user"]) => `${user.first_name} ${user.last_name}`.trim();

// Lo que hay dentro del panel abierto. Solo existe mientras está abierto: al cerrarlo, su
// lectura se cancela y se olvida, y la siguiente apertura vuelve a preguntar desde cero.
function Members({
  slug,
  team,
  manages,
  onGone,
  focusClose,
}: {
  slug: string;
  team: Team;
  manages: boolean;
  onGone: (notice?: string) => void; // 404: el equipo ya no está al alcance; con su aviso o sin él
  focusClose: () => void;
}) {
  const t = useTranslations("teams.members");
  const errors = useTranslations("errors");
  const { membership_id: own } = useTenant();
  // A quién se acaba de quitar desde aquí: su fila ya no está para decirlo.
  const [removed, setRemoved] = useState<string | null>(null);
  const membersKey = [...getTeamMembersListQueryKey(slug, team.id), "all"];
  const members = useQuery<TeamMember[], ApiError>({
    queryKey: membersKey,
    queryFn: async ({ signal }) => {
      try {
        return await allMembers(slug, team.id, signal);
      } catch (error) {
        // Una respuesta que llega con el panel ya cerrado no avisa de nada.
        if (error instanceof ApiError && error.status === 404 && !signal.aborted) onGone();
        throw error;
      }
    },
    staleTime: 0,
    gcTime: 0,
    refetchOnWindowFocus: false, // no por su cuenta: «Reintentar» se desmontaría con el foco
    refetchOnReconnect: false,
  });
  const retryButton = useRef<HTMLButtonElement>(null);
  const [retrying, setRetrying] = useState(false);
  async function retry() {
    setRetrying(true);
    const result = await members.refetch();
    setRetrying(false);
    // El botón se va al llegar la lista o una negativa: el foco, a «Cerrar», si seguía aquí o
    // en ninguna parte.
    const active = document.activeElement;
    const left = result.isSuccess || result.error?.code === "PERMISSION_DENIED";
    if (left && (active === document.body || active === retryButton.current)) {
      focusClose();
    }
  }

  // Sin sesión (401), `Providers` lleva al login; con un 404 el panel se cierra: en ninguno de
  // los dos casos se enseña un error aquí.
  const error = members.error && ![401, 404].includes(members.error.status) ? members.error : null;
  if (members.data && !retrying) {
    return (
      <>
        {members.data.length > 0 ? (
          <ul aria-label={t("panel", { team: team.name })} className="flex flex-col gap-2">
            {members.data.map((member) => (
              <li
                key={member.id}
                className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5"
              >
                <p className="flex min-w-0 flex-col text-sm leading-snug">
                  <span className="wrap-anywhere">
                    {fullName(member.user) || member.user.email}
                  </span>
                  {fullName(member.user) ? (
                    <span className="text-muted wrap-anywhere">{member.user.email}</span>
                  ) : null}
                </p>
                <p className="text-muted flex flex-wrap gap-x-2 text-sm">
                  <span>{named(ROLES, member.team_role)}</span>
                  {/* El estado en la organización, solo si no es el habitual. */}
                  {member.status === "ACTIVE" ? null : (
                    <span>{t("status", { status: named(STATUSES, member.status) })}</span>
                  )}
                </p>
                {/* Nadie se quita a sí mismo: la API no lo deja, y aquí no se ofrece. */}
                {manages && member.id !== own ? (
                  <TeamMemberRemove
                    slug={slug}
                    team={team}
                    member={member}
                    membersKey={membersKey}
                    onRemoved={setRemoved}
                    onGone={onGone}
                    focusClose={focusClose}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        <p role="status" className="text-muted text-sm">
          {t("count", { count: members.data.length })}
        </p>
        {/* Montado desde que se abre el panel de quien administra: un lector de pantalla
            anuncia el resultado cuando cambia. */}
        {manages ? (
          <p role="status" className={removed ? "text-sm wrap-anywhere" : "sr-only"}>
            {removed ? t("remove.done", { name: removed, team: team.name }) : ""}
          </p>
        ) : null}
        {manages ? (
          <TeamMemberAdd
            slug={slug}
            team={team}
            present={new Set(members.data.map((member) => member.id))}
            membersKey={membersKey}
            onGone={onGone}
          />
        ) : null}
      </>
    );
  }
  if (error?.code === "PERMISSION_DENIED") {
    return (
      <p role="alert" className="text-danger text-sm">
        {t("denied")}
      </p>
    );
  }
  if (error || retrying) {
    return (
      <div className="flex flex-col items-start gap-2">
        {error ? (
          <p role="alert" className="text-danger text-sm">
            {errors(`api.${apiErrorKey(error)}`)}
          </p>
        ) : null}
        {/* No se desmonta mientras reintenta: conserva el foco. */}
        <Button
          ref={retryButton}
          variant="primary"
          className="min-h-11 sm:min-h-9"
          aria-disabled={retrying}
          onClick={() => retrying || void retry()}
        >
          {retrying ? t("loading") : errors("retry")}
        </Button>
      </div>
    );
  }
  return (
    <p role="status" className="text-muted text-sm">
      {t("loading")}
    </p>
  );
}

// Quién forma un equipo (F2-64), con `GET …/teams/{id}/members/` (F2-55). Nada se pide hasta
// abrir el panel y cada apertura vuelve a preguntar. Quién puede verlo lo decide la API, que
// exige ver equipos y ver personas. La única escritura es incorporar (F2-65, `TeamMemberAdd`).
export function TeamMembersPanel({
  slug,
  team,
  manages,
  onAsk,
  onStale,
}: {
  slug: string;
  team: Team;
  manages: boolean; // comodidad: quien administra equipos puede además incorporar (F2-65)
  onAsk: () => void; // se abre el panel: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta tarjeta
}) {
  const t = useTranslations("teams.members");
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = useRef<HTMLButtonElement>(null);

  const opened = useRef(false);
  useEffect(() => {
    // El control pulsado desaparece al abrir y al cerrar: el foco pasa al que lo sustituye,
    // salvo que el usuario ya esté en otra parte.
    if (document.activeElement === document.body) {
      if (open) close.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function gone(notice?: string) {
    // El equipo ya no está al alcance: lo explica la lista, que se vuelve a pedir.
    const active = document.activeElement;
    // La tarjeta entera puede desaparecer: también si el foco estaba en otra acción suya.
    const here = active === document.body || !!root.current?.closest("li")?.contains(active);
    onStale(notice ?? t("stale", { team: team.name }), here);
    setOpen(false);
  }

  const who = { team: team.name, slug: team.slug }; // dos equipos pueden llamarse igual
  return (
    <div
      ref={root}
      // Abierto ocupa su fila, debajo de las demás acciones de la tarjeta.
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: abriría y cerraría el panel sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <div
          role="group"
          aria-label={t("panelLabel", who)}
          className="flex w-full max-w-xl flex-col gap-2"
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm font-medium wrap-anywhere">{t("panel", { team: team.name })}</p>
            <Button
              ref={close}
              variant="ghost"
              className="min-h-11 shrink-0 sm:min-h-9"
              onClick={() => setOpen(false)}
            >
              {t("close")}
            </Button>
          </div>
          <Members
            slug={slug}
            team={team}
            manages={manages}
            onGone={gone}
            focusClose={() => close.current?.focus()}
          />
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", who)}
          onClick={() => {
            onAsk();
            setOpen(true);
          }}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
