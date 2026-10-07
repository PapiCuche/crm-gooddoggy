"use client";

import { useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { allPages } from "@/lib/all-pages";
import {
  getInvitationsListQueryKey,
  getRolesListQueryKey,
  invitationsList,
  rolesList,
} from "@/lib/api/client";
import type { Invitation, InvitationStatusEnum, Role } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";
import { text } from "@/lib/message-text";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";
import { InvitationCreate } from "./invitation-create";
import { InvitationRevokeAction } from "./invitation-revoke-action";

// Los estados son un enum del contrato: si gana uno y el catálogo no, no compila. Uno que esta
// versión no conoce (una API más nueva), o que ni es un texto, se enseña como llega.
const STATUSES: Record<InvitationStatusEnum, string> = messages.invitations.status;
const named = (code: unknown) => text(STATUSES, String(code)) ?? String(code);
// El tono, igual: por propiedades propias. `TONE["constructor"]` no es un texto ni es `undefined`.
const TONE = { ACCEPTED: "text-success", PENDING: "" };
const tone = (code: unknown) => text(TONE, String(code)) ?? "text-muted";

// Invitaciones (F2-83): lo que devuelve `GET /api/v1/o/{slug}/invitations/`, de la más reciente
// a la más antigua. La lista, sus estados y su foco son los de `CursorList`. Quién puede verlas
// lo decide la API. Encima, «Invitar» (F2-84); en cada fila pendiente o caducada, «Revocar» (F2-85).
export function InvitationsList() {
  const t = useTranslations("invitations");
  const format = useFormatter();
  const { organization, permissions } = useTenant();
  // Cómo se llama cada rol: la invitación solo trae identificadores. A quien la API dijo que
  // puede ver los roles se le pide el directorio y se empareja aquí; a quien no, ni se le pide.
  // Si falla o tarda, la lista funciona igual y dice cuántos roles son: no es su error.
  const seesRoles = permissions.some((grant) => grant.code === "roles.view");
  const roles = useQuery<Role[], ApiError>({
    queryKey: [...getRolesListQueryKey(organization.slug), "names"],
    queryFn: ({ signal }) =>
      allPages((cursor) =>
        rolesList(organization.slug, { limit: 200, ...(cursor ? { cursor } : {}) }, { signal }),
      ),
    enabled: seesRoles,
    gcTime: 0, // como la lista: al salir no queda en memoria
  });
  // Comodidad: «Invitar» se ofrece a quien la API dijo que puede invitar y además ve los roles
  // entre los que elegir.
  const has = (code: string) => permissions.some((grant) => grant.code === code);
  const revokes = has("users.invite") && has("users.manage"); // lo que pide la API
  const invites = seesRoles && revokes;
  const list = useRef<CursorListHandle>(null);
  // Lo que «Revocar» dejó dicho (F2-85). Vive aquí: al terminar, la acción desaparece de su
  // fila. `stale`: la pantalla ya no reflejaba a la API, y la lista se vuelve a pedir.
  const [notice, setNotice] = useState<{ text: string; stale: boolean } | null>(null);
  function settle(text: string, stale: boolean, here: boolean) {
    setNotice({ text, stale });
    if (stale) list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const listKey = [...getInvitationsListQueryKey(organization.slug), "pages"];
  const names = seesRoles && roles.data ? new Map(roles.data.map((r) => [r.id, r.name])) : null;
  const day = (when: string) => format.dateTime(new Date(when), { dateStyle: "medium" });

  return (
    <CursorList<Invitation>
      ref={list}
      section="invitations"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        invitationsList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        <>
          {invites ? (
            <InvitationCreate
              slug={organization.slug}
              listKey={listKey}
              roles={roles.data}
              rolesFailed={roles.isError}
              onAsk={() => {
                setNotice(null);
                // Cada apertura vuelve a preguntar: el directorio pudo fallar, o un rol, borrarse.
                void roles.refetch({ cancelRefetch: false });
              }}
              onDone={() => setNotice(null)}
            />
          ) : null}
          {notice?.stale ? (
            <p role="alert" className="text-danger wrap-anywhere">
              {notice.text}
            </p>
          ) : null}
          {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
          {revokes ? (
            <p
              role="status"
              className={notice && !notice.stale ? "text-sm wrap-anywhere" : "sr-only"}
            >
              {notice && !notice.stale ? notice.text : ""}
            </p>
          ) : null}
        </>
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_minmax(0,1fr)] sm:items-start"
    >
      {(invitation) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">{invitation.email}</span>
            <span className="text-muted text-sm">
              {t("invited", { date: day(invitation.created_at) })}
            </span>
          </p>
          {names ? (
            <ul aria-label={t("roles")} className="flex min-w-0 flex-wrap gap-1.5">
              {invitation.role_ids.map((role, place) => (
                // Por posición: una invitación puede repetir un rol, y la lista no cambia.
                <li
                  key={place}
                  className={cn(
                    "rounded-md px-2 py-0.5 text-sm wrap-anywhere",
                    names.has(role) ? "bg-background" : "text-muted",
                  )}
                >
                  {names.get(role) ?? t("goneRole")}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted text-sm">
              {t("roleCount", { count: invitation.role_ids.length })}
            </p>
          )}
          <p className="flex flex-col text-sm sm:items-end sm:text-right">
            <span className={cn("font-medium wrap-anywhere", tone(invitation.status))}>
              {named(invitation.status)}
            </span>
            {invitation.status === "PENDING" || invitation.status === "EXPIRED" ? (
              <span className="text-muted">
                {t(invitation.status === "PENDING" ? "expires" : "expiry", {
                  date: day(invitation.expires_at),
                })}
              </span>
            ) : null}
          </p>
          {revokes ? (
            <InvitationRevokeAction
              slug={organization.slug}
              invitation={invitation}
              listKey={listKey}
              onAsk={() => setNotice(null)}
              onSettle={settle}
              onLost={() => list.current?.focusHeading()}
            />
          ) : null}
        </>
      )}
    </CursorList>
  );
}
