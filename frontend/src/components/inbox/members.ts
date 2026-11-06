"use client";

import { useQuery } from "@tanstack/react-query";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { getMembersListQueryKey, membersList } from "@/lib/api/client";

// Los miembros de la organización para nombrar al responsable de una conversación y elegir a
// quien la recibe. La lista exige `users.view`; sin él no hay nombres ni destinos y la pantalla lo
// dice con menos detalle («Asignada»). Solo los activos son destino; el servidor valida de nuevo
// (cualquier destino inválido es el mismo 400). Nada queda entre visitas.
export function useMembers() {
  const { organization, permissions, user } = useTenant();
  const slug = organization.slug;
  const people = useQuery({
    queryKey: [...getMembersListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => membersList(slug, params, { signal })),
    enabled: permissions.some((grant) => grant.code === "users.view"),
    gcTime: 0,
  });
  const names = new Map(
    people.data?.map(({ user: one }) => [
      one.id,
      `${one.first_name} ${one.last_name}`.trim() || one.email,
    ]),
  );
  return {
    name: (id: string): string | null => names.get(id) ?? null,
    // A quién se puede transferir: activos, sin contar a quien escribe.
    targets: (people.data ?? [])
      .filter(({ status, user: one }) => status === "ACTIVE" && one.id !== user.id)
      .map(({ user: one }): [string, string] => [one.id, names.get(one.id) ?? one.email]),
  };
}
