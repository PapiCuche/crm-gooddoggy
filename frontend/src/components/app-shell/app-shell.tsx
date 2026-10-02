"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect } from "react";

import { cn } from "@/lib/utils";

import { MobileMenu } from "./mobile-menu";
import { NAVIGATION, visibleItems } from "./navigation";
import { SessionActions } from "./session-actions";
import { useTenant } from "./tenant-context";

function displayName(user: { first_name: string; last_name: string; email: string }): string {
  return `${user.first_name} ${user.last_name}`.trim() || user.email;
}

// Barra lateral del workspace (Figma GOOD DOGGY, 5:212): fija en escritorio y dentro del
// menú en móvil. `label` distingue las dos copias de la navegación para los lectores de pantalla.
function Sidebar({ label, current }: { label: string; current: string }) {
  const t = useTranslations();
  const { organization, permissions, roles, user } = useTenant();
  const base = `/o/${organization.slug}`;
  return (
    <div className="bg-surface flex h-full w-56 flex-col overflow-y-auto">
      <p className="flex items-center gap-2 px-6 pt-6">
        <span aria-hidden className="text-[27px] leading-none">
          GD
        </span>
        <span className="flex flex-col text-[13px] leading-tight tracking-wide uppercase">
          <span className="text-[15px] font-medium">{t("app.brand")}</span>
          {t("shell.workspace")}
        </span>
      </p>
      <p className="border-border mx-4 mt-5 flex flex-col rounded-lg border px-3 py-2.5 leading-snug">
        <span className="truncate font-medium">{organization.name}</span>
        <span className="text-muted truncate font-mono text-[13px]">{organization.slug}</span>
      </p>
      <nav aria-label={label} className="mx-4 mt-5">
        <ul className="flex flex-col gap-1">
          {visibleItems(NAVIGATION, permissions).map((item) => {
            const href = base + item.path;
            const active = current === href;
            return (
              <li key={item.key}>
                <Link
                  href={href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "flex h-11 items-center gap-3 rounded-lg border border-transparent px-3 lg:h-[37px]",
                    active
                      ? "border-foreground/10 bg-accent text-accent-foreground"
                      : "hover:bg-surface-raised active:bg-surface-raised",
                  )}
                >
                  <span
                    aria-hidden
                    className={cn("size-3 rounded-[3px]", active ? "bg-surface" : "bg-foreground")}
                  />
                  {t(`shell.nav.${item.key}`)}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
      <div className="border-border mx-4 mt-auto flex flex-col gap-3 border-t py-4">
        <p className="flex flex-col px-3 leading-snug">
          <span className="truncate font-medium">{displayName(user)}</span>
          <span className="text-muted truncate text-[13px]">
            {roles.map((role) => role.name).join(" · ") || t("shell.noRole")}
          </span>
        </p>
        <SessionActions />
      </div>
    </div>
  );
}

// App Shell (barra lateral + barra superior + workspace) de una organización. Lo que muestra
// sale del contexto que devolvió la API; la navegación solo lista lo que el usuario puede abrir.
export function AppShell({ children }: { children: ReactNode }) {
  const t = useTranslations();
  const pathname = usePathname().replace(/\/$/, ""); // `/o/acme/` es la misma página
  const { organization } = useTenant();
  const item = NAVIGATION.find((entry) => pathname === `/o/${organization.slug}${entry.path}`);
  const title = `${organization.name} · ${t("app.name")}`;
  useEffect(() => {
    document.title = title; // la pestaña dice en qué organización se está
  }, [title]);
  return (
    <div className="flex min-h-dvh">
      <a
        href="#workspace"
        className="bg-foreground text-surface sr-only z-50 rounded-[12px] focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:px-4 focus:py-2"
      >
        {t("app.skipToContent")}
      </a>
      <aside className="sticky top-0 hidden h-dvh shrink-0 lg:block">
        <Sidebar label={t("shell.navigation")} current={pathname} />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="border-border flex h-[60px] shrink-0 items-center gap-2 border-b px-4 lg:h-[70px] lg:border-l lg:px-8">
          <MobileMenu
            label={t("shell.menu")}
            title={t("shell.menuTitle")}
            closeLabel={t("shell.closeMenu")}
          >
            <Sidebar label={t("shell.menuNavigation")} current={pathname} />
          </MobileMenu>
          <p className="shrink-0">
            {t("shell.workspace")}
            {item ? ` › ${t(`shell.nav.${item.key}`)}` : null}
          </p>
          <span className="text-muted ml-auto min-w-0 truncate text-sm lg:hidden">
            {organization.name}
          </span>
        </header>
        <main
          id="workspace"
          tabIndex={-1}
          className="flex-1 px-4 py-8 focus-visible:outline-none lg:px-8"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
