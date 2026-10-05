"use client";

import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { getRolesListQueryKey, rolesList } from "@/lib/api/client";
import type { Role } from "@/lib/api/model";

// Los códigos del catálogo tienen esta forma (`modulo.accion`); cualquier otro se enseña tal cual.
const CODE = /^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$/;

// Directorio de roles (F2-23): lo que devuelve `GET /api/v1/o/{slug}/roles/`. Solo muestra: nada
// decide aquí por el nombre o el código de un rol, y qué puede ver cada quien lo decide la API.
export function RolesList() {
  const t = useTranslations();
  const { organization } = useTenant();
  // Un permiso que estos textos no conocen se enseña con su código: no se oculta.
  const label = (code: string) =>
    CODE.test(code) && t.has(`catalog.${code}` as never) ? t(`catalog.${code}` as never) : code;

  return (
    <CursorList<Role>
      section="roles"
      organization={organization.name}
      listKey={[...getRolesListQueryKey(organization.slug), "pages"]}
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
          {role.description ? (
            <p className="text-muted text-sm wrap-anywhere">{role.description}</p>
          ) : null}
          <ul
            aria-label={t("roles.permissions", { role: role.name })}
            className="flex flex-col gap-1"
          >
            {role.permissions.map((grant) => (
              <li key={grant.code} className="flex flex-wrap gap-x-2 text-sm">
                <span className="wrap-anywhere">{label(grant.code)}</span>
                {grant.scope ? (
                  <span className="text-muted">{t(`roles.scope.${grant.scope}`)}</span>
                ) : null}
              </li>
            ))}
            {role.permissions.length === 0 ? (
              <li className="text-muted text-sm">{t("roles.noPermission")}</li>
            ) : null}
          </ul>
        </>
      )}
    </CursorList>
  );
}
