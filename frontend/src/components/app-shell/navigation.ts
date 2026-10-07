import type { Grant } from "@/lib/api/model";

// Entradas de la navegación del workspace. `permission` es el permiso del catálogo que da
// sentido a la entrada: sin él no se muestra. Si son varios, hacen falta todos, como en la ruta
// de la API que la pantalla lee. Es comodidad, no seguridad: la API decide en cada petición.
// Solo se listan módulos que ya existen; cada fase añade los suyos.
export type NavigationItem = {
  key: "home" | "members" | "invitations" | "roles" | "branches" | "teams" | "audit";
  path: string;
  permission?: string | readonly string[];
};

export const NAVIGATION: readonly NavigationItem[] = [
  { key: "home", path: "" },
  { key: "members", path: "/miembros", permission: "users.view" },
  { key: "invitations", path: "/invitaciones", permission: ["users.invite", "users.manage"] },
  { key: "roles", path: "/roles", permission: "roles.view" },
  { key: "branches", path: "/sucursales", permission: "organization.view" },
  { key: "teams", path: "/equipos", permission: "teams.view" },
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
