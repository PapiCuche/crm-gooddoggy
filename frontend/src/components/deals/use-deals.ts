"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import {
  getMembersListQueryKey,
  getPipelinesListQueryKey,
  membersList,
  pipelinesList,
} from "@/lib/api/client";

// Los pipelines activos, el predeterminado primero (después, por su orden). Se piden enteros
// (`everyPage`): son pocos y el selector los necesita todos. Nada queda entre visitas.
export function usePipelines(slug: string) {
  return useQuery({
    queryKey: [...getPipelinesListQueryKey(slug), "all"],
    queryFn: async ({ signal }) =>
      (await everyPage((params) => pipelinesList(slug, params, { signal })))
        .filter((pipeline) => pipeline.is_active)
        .sort((a, b) => Number(b.is_default) - Number(a.is_default) || a.sort_order - b.sort_order),
    gcTime: 0,
  });
}

// El responsable llega como identificador. Se nombra con el directorio solo si la API dijo que
// el usuario puede leerlo (`users.view`); si no, o si falla, se dice con menos detalle.
export function useOwners() {
  const t = useTranslations("deals.owner");
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
    // Quien filtra siempre puede elegirse a sí mismo; el resto, solo si puede leer el directorio.
    options: [...names].filter(([id]) => id !== user.id),
    name(id: string | null): string {
      if (!id) return t("none");
      if (id === user.id) return t("you");
      return names.get(id) ?? (people.isLoading ? t("pending") : t("other"));
    },
  };
}
