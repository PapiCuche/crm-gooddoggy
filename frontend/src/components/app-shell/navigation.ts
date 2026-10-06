import type { Grant } from "@/lib/api/model";

// Entradas de la navegación del workspace. `permission` es el permiso del catálogo que da
// sentido a la entrada: sin él no se muestra. Es comodidad, no seguridad: la API decide en
// cada petición. Solo se listan módulos que ya existen; cada fase añade los suyos.
export type NavigationItem = {
  key: "home" | "members" | "roles" | "branches" | "teams";
  path: string;
  permission?: string;
};

export const NAVIGATION: readonly NavigationItem[] = [
  { key: "home", path: "" },
  { key: "members", path: "/miembros", permission: "users.view" },
  { key: "roles", path: "/roles", permission: "roles.view" },
  { key: "branches", path: "/sucursales", permission: "organization.view" },
  { key: "teams", path: "/equipos", permission: "teams.view" },
];

export function visibleItems<Item extends { permission?: string }>(
  items: readonly Item[],
  permissions: readonly Grant[],
): Item[] {
  const held = new Set(permissions.map((grant) => grant.code));
  return items.filter((item) => item.permission === undefined || held.has(item.permission));
}
