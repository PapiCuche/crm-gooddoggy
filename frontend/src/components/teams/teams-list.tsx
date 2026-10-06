"use client";

import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { getTeamsListQueryKey, teamsList } from "@/lib/api/client";
import type { AssignmentStrategyEnum, Team } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";
import { TeamCreate } from "./team-create";

// Las estrategias con nombre propio. Si el contrato gana una y el catálogo no, no compila.
const STRATEGIES: Record<AssignmentStrategyEnum, string> = messages.teams.strategy;

// Equipos (F2-60, F2-61): lo que devuelve `GET /api/v1/o/{slug}/teams/`, página a página. La lista,
// sus estados y su foco son los de `CursorList`. Quién puede verlos lo decide la API.
export function TeamsList() {
  const t = useTranslations();
  const { organization, permissions } = useTenant();
  // Comodidad: «Crear equipo» se ofrece a quien la API dijo que tiene `teams.manage`.
  const canManage = permissions.some((grant) => grant.code === "teams.manage");
  const listKey = [...getTeamsListQueryKey(organization.slug), "pages"];

  return (
    <CursorList<Team>
      section="teams"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        teamsList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={canManage ? <TeamCreate slug={organization.slug} listKey={listKey} /> : null}
      empty={<p className="text-muted">{t("teams.empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] sm:items-start"
    >
      {(team) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{team.name}</span>
            <span className="text-muted text-sm wrap-anywhere">{team.slug}</span>
          </p>
          <div className="flex min-w-0 flex-col text-sm">
            {team.description.trim() ? (
              <p className="wrap-anywhere">{team.description}</p>
            ) : (
              <p className="text-muted">{t("teams.noDescription")}</p>
            )}
            <p className="text-muted wrap-anywhere">
              {t("teams.assignment", {
                // Una estrategia que esta versión no conoce se enseña con su código. Solo cuentan
                // las claves propias del catálogo, como en `apiErrorKey`: `t.has` también da por
                // buena `constructor` o `MANUAL.length`, y entonces se vería la clave.
                strategy: Object.hasOwn(STRATEGIES, team.assignment_strategy)
                  ? t(`teams.strategy.${team.assignment_strategy}`)
                  : team.assignment_strategy,
              })}
            </p>
          </div>
          <p
            className={cn(
              "text-sm font-medium sm:text-right",
              team.is_active ? "text-success" : "text-muted",
            )}
          >
            {t(team.is_active ? "teams.active" : "teams.inactive")}
          </p>
        </>
      )}
    </CursorList>
  );
}
