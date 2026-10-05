"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { MemberRolesAction } from "@/components/members/member-roles-action";
import { MemberStatusAction } from "@/components/members/member-status-action";
import { getMembersListQueryKey, membersList } from "@/lib/api/client";
import type { Member } from "@/lib/api/model";
import { cn } from "@/lib/utils";

function fullName(user: Member["user"]): string {
  return `${user.first_name} ${user.last_name}`.trim();
}

// Directorio de miembros (F2-17): lo que devuelve `GET /api/v1/o/{slug}/members/`, página a
// página. La lista, sus estados y su foco son los de `CursorList`.
export function MembersList() {
  const t = useTranslations();
  const format = useFormatter();
  const { organization, membership_id: own, permissions } = useTenant();
  // Comodidad, no autorización: sin el permiso la API responde 403 a la acción (ADR-017).
  const can = (code: string) => permissions.some((grant) => grant.code === code);
  const manages = can("users.manage");
  const assigns = manages && can("roles.view"); // el panel de roles necesita verlos
  const listKey = [...getMembersListQueryKey(organization.slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  // Una acción respondió que la pantalla ya no refleja a la API (F2-21). El aviso vive aquí:
  // la acción, o su fila entera, puede desaparecer cuando llega la lista nueva.
  const [notice, setNotice] = useState<string | null>(null);
  function stale(text: string, here: boolean) {
    setNotice(text);
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }

  return (
    <CursorList<Member>
      ref={list}
      section="members"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        membersList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        notice ? (
          <p role="alert" className="text-danger">
            {notice}
          </p>
        ) : null
      }
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_auto] sm:items-center"
    >
      {(member) => (
        <>
          <p className="flex min-w-0 flex-col leading-snug">
            <span className="font-medium wrap-anywhere">
              {fullName(member.user) || member.user.email}
            </span>
            {fullName(member.user) ? (
              <span className="text-muted text-sm wrap-anywhere">{member.user.email}</span>
            ) : null}
          </p>
          <ul aria-label={t("members.roles")} className="flex flex-wrap gap-1.5">
            {member.roles.map((role) => (
              <li key={role.code} className="bg-background rounded-md px-2 py-0.5 text-sm">
                {role.name}
              </li>
            ))}
            {member.roles.length === 0 ? (
              <li className="text-muted text-sm">{t("members.noRole")}</li>
            ) : null}
          </ul>
          <p className="flex flex-col text-sm sm:items-end">
            <span
              className={cn(
                "font-medium",
                member.status === "ACTIVE" ? "text-success" : "text-muted",
              )}
            >
              {t(`members.status.${member.status}`)}
            </span>
            <span className="text-muted">
              {t("members.joined", {
                date: format.dateTime(new Date(member.joined_at), { dateStyle: "medium" }),
              })}
            </span>
          </p>
          {manages &&
          member.id !== own &&
          (member.status === "ACTIVE" || member.status === "SUSPENDED") ? (
            <MemberStatusAction
              slug={organization.slug}
              member={member}
              name={fullName(member.user) || member.user.email}
              organization={organization.name}
              listKey={listKey}
              onAsk={() => setNotice(null)}
              onStale={stale}
            />
          ) : null}
          {assigns && member.id !== own ? (
            <MemberRolesAction
              slug={organization.slug}
              member={member}
              name={fullName(member.user) || member.user.email}
              listKey={listKey}
              onAsk={() => setNotice(null)}
              onStale={stale}
            />
          ) : null}
        </>
      )}
    </CursorList>
  );
}
