import type { Grant } from "@/lib/api/model";

// Entradas de la navegación del workspace. `permission` es el permiso del catálogo que da
// sentido a la entrada: sin él no se muestra. Si son varios, hacen falta todos, como en la ruta
// de la API que la pantalla lee. Es comodidad, no seguridad: la API decide en cada petición.
// Solo se listan módulos que ya existen; cada fase añade los suyos.
export type NavigationItem = {
  key: "home" | "members" | "invitations" | "roles" | "branches" | "teams" | "audit" | "catalog";
  path: string;
  permission?: string | readonly string[];
  section?: string; // las páginas bajo esta ruta también son de la entrada (el catálogo)
};

export const NAVIGATION: readonly NavigationItem[] = [
  { key: "home", path: "" },
  { key: "members", path: "/miembros", permission: "users.view" },
  { key: "invitations", path: "/invitaciones", permission: ["users.invite", "users.manage"] },
  { key: "roles", path: "/roles", permission: "roles.view" },
  { key: "branches", path: "/sucursales", permission: "organization.view" },
  { key: "teams", path: "/equipos", permission: "teams.view" },
  { key: "catalog", path: "/catalogo/marcas", section: "/catalogo", permission: "products.view" },
  { key: "audit", path: "/auditoria", permission: "audit.view" },
];

export function visibleItems<Item extends { permission?: string | readonly string[] }>(
  items: readonly Item[],
  permissions: readonly Grant[],
): Item[] {
  const held = new Set(permissions.map((grant) => grant.code));
  const needs = ({ permission = [] }: Item) =>
    typeof permission === "string" ? [permission] : permission;
  return items.filter((item) => needs(item).every((code) => held.has(code)));
}

// Si la ruta `path` es de la entrada: su página, o una de su sección. `base`: `/o/{slug}`.
export function holds(item: NavigationItem, base: string, path: string): boolean {
  return (
    path === base + item.path || (!!item.section && path.startsWith(`${base}${item.section}/`))
  );
}
