"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { cn } from "@/lib/utils";

// Las páginas del catálogo. Todas leen con `products.view`, el permiso de la entrada «Catálogo».
const PAGES = [
  { key: "brands", path: "/catalogo/marcas" },
  { key: "categories", path: "/catalogo/categorias" },
  { key: "products", path: "/catalogo/productos" },
] as const;

// Navegación entre las páginas del catálogo (F3-05), encima de cada una.
export const CatalogNav = () => <SectionNav section="catalog" pages={PAGES} />;

// Las pestañas de una entrada del menú con varias páginas (catálogo, inventario, precios, clientes): `section` es
// su espacio de mensajes en `shell`, con `label` y el nombre de cada página.
export function SectionNav({
  section,
  pages,
}: {
  section: "catalog" | "inventory" | "prices" | "contacts";
  pages: readonly { key: string; path: string }[];
}) {
  const t = useTranslations(`shell.${section}`);
  const { organization } = useTenant();
  const pathname = usePathname().replace(/\/$/, "");
  return (
    <nav aria-label={t("label")} className="mb-6">
      <ul className="flex flex-wrap gap-2">
        {pages.map((page) => {
          const href = `/o/${organization.slug}${page.path}`;
          const current = pathname === href;
          return (
            <li key={page.key}>
              <Link
                href={href}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "flex h-11 items-center rounded-lg border px-4 text-sm lg:h-9",
                  current
                    ? "border-foreground/10 bg-accent text-accent-foreground"
                    : "border-border hover:bg-surface-raised active:bg-surface-raised",
                )}
              >
                {t(page.key as "label")}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
