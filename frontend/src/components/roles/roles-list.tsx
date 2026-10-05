"use client";

import { useMessages, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { getRolesListQueryKey, rolesList } from "@/lib/api/client";
import type { Role } from "@/lib/api/model";

import { RoleCreate } from "./role-create";
import { RoleDeleteAction } from "./role-delete-action";
import { RoleEditAction } from "./role-edit-action";
import { RolePermissionsAction } from "./role-permissions-action";

// El texto de `messages` para `code` (`users.manage` es `messages.users.manage`), si existe y es
// un texto. El código viene de la API: se busca por propiedades propias, paso a paso, y no como
// una ruta de mensajes, que resolvería también `users` (un objeto) o `users.constructor.name`.
function text(messages: unknown, code: string): string | null {
  let node = messages;
  for (const part of code.split(".")) {
    if (typeof node !== "object" || node === null || !Object.hasOwn(node, part)) return null;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : null;
}

// Directorio de roles (F2-23): lo que devuelve `GET /api/v1/o/{slug}/roles/`. Nada decide aquí
// por el nombre o el código de un rol, y qué puede ver o hacer cada quien lo decide la API.
export function RolesList() {
  const t = useTranslations();
  const { organization, permissions } = useTenant();
  // Comodidad: «Crear rol» se ofrece a quien la API dijo que tiene `roles.manage` (F2-35).
  const canManage = permissions.some((grant) => grant.code === "roles.manage");
  const listKey = [...getRolesListQueryKey(organization.slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  // Un cambio de permisos respondió que la pantalla ya no refleja a la API (F2-36). El aviso
  // vive aquí: el panel, o la tarjeta entera, puede desaparecer cuando llega la lista nueva.
  const [notice, setNotice] = useState<string | null>(null);
  // Un rol borrado (F2-40) también se anuncia aquí: su tarjeta ya no está para decirlo.
  const [done, setDone] = useState<string | null>(null);
  function stale(text: string, here: boolean) {
    setNotice(text);
    setDone(null);
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  function ask() {
    setNotice(null);
    setDone(null);
  }
  // Un permiso, o un alcance, que estos textos no conocen se enseña con su código: no se oculta.
  const { catalog, roles } = useMessages();
  const label = (code: string) => text(catalog, code) ?? code;
  const scope = (code: string) => text(roles, `scope.${code}`) ?? code;

  return (
    <CursorList<Role>
      ref={list}
      section="roles"
      organization={organization.name}
      listKey={listKey}
      notice={
        <>
          {canManage ? <RoleCreate slug={organization.slug} listKey={listKey} onAsk={ask} /> : null}
          {notice ? (
            <p role="alert" className="text-danger">
              {notice}
            </p>
          ) : null}
          {/* Montado desde el principio para quien puede borrar: un lector de pantalla anuncia
              el resultado cuando cambia. */}
          {canManage ? (
            <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
              {done ?? ""}
            </p>
          ) : null}
        </>
      }
      fetchPage={(cursor, signal) =>
        rolesList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      rowClassName="flex flex-col gap-3"
    >
      {(role) => (
        <>
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <p className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="font-medium wrap-anywhere">{role.name}</span>
              <span className="bg-background rounded-md px-2 py-0.5 text-sm">
                {t(role.is_system ? "roles.system" : "roles.custom")}
              </span>
            </p>
            <p className="text-muted text-sm">{t("roles.members", { count: role.members })}</p>
          </div>
          {role.description.trim() ? (
            <p className="text-muted text-sm wrap-anywhere">{role.description}</p>
          ) : null}
          <ul
            aria-label={t("roles.permissions", { role: role.name })}
            className="flex flex-col gap-1"
          >
            {role.permissions.map((grant) => (
              <li key={grant.code} className="flex flex-wrap gap-x-2 text-sm">
                <span className="wrap-anywhere">{label(grant.code)}</span>
                {grant.scope ? <span className="text-muted">{scope(grant.scope)}</span> : null}
              </li>
            ))}
            {role.permissions.length === 0 ? (
              <li className="text-muted text-sm">{t("roles.noPermission")}</li>
            ) : null}
          </ul>
          {/* Comodidad: la API marca los roles que no admite editar (el Owner, uno propio). */}
          {canManage && role.editable ? (
            <div className="flex flex-wrap items-start gap-2">
              <RoleEditAction
                slug={organization.slug}
                role={role}
                listKey={listKey}
                onAsk={ask}
                onStale={stale}
              />
              <RolePermissionsAction
                slug={organization.slug}
                role={role}
                listKey={listKey}
                label={label}
                scope={scope}
                onAsk={ask}
                onStale={stale}
              />
              {/* Una plantilla no se borra: la API lo dice con `is_system` y lo impone. */}
              {role.is_system ? null : (
                <RoleDeleteAction
                  slug={organization.slug}
                  role={role}
                  listKey={listKey}
                  onAsk={ask}
                  onGone={(text, how) => {
                    if (how.stale) return stale(text, how.here);
                    setNotice(null);
                    setDone(text);
                    if (how.here) list.current?.focusHeading();
                  }}
                />
              )}
            </div>
          ) : null}
        </>
      )}
    </CursorList>
  );
}
