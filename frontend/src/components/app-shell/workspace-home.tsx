"use client";

import { useTranslations } from "next-intl";

import { useTenant } from "./tenant-context";

// Inicio del workspace: lo que la API dice del usuario en esta organización. Los módulos de
// negocio llegan con sus fases; aquí no hay datos de ejemplo.
export function WorkspaceHome() {
  const t = useTranslations("home");
  const { organization, roles, user } = useTenant();
  return (
    <section className="flex max-w-2xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">
          {user.first_name ? t("title", { name: user.first_name }) : t("titleNoName")}
        </h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      <div className="border-border bg-surface flex flex-col gap-2 rounded-lg border p-5">
        <h2 className="font-medium">{t("accessTitle")}</h2>
        {roles.length > 0 ? (
          <ul className="flex flex-wrap gap-2">
            {roles.map((role) => (
              <li key={role.code} className="bg-background rounded-md px-2.5 py-1 text-sm">
                {role.name}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-muted">{t("noRole")}</p>
        )}
        <p className="text-muted text-sm">{t("modules")}</p>
      </div>
    </section>
  );
}
