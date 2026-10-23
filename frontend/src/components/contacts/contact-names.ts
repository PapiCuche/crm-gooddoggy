"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import {
  branchesList,
  getBranchesListQueryKey,
  getMembersListQueryKey,
  membersList,
} from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";

// El responsable y la sucursal de un contacto llegan como identificadores. Se nombran con el
// directorio y con las sucursales solo si la API dijo que el usuario puede leerlos
// (`users.view`, `organization.view`) y además ve los contactos; si no, o si fallan, se dice
// con menos detalle. Lo comparten la lista (F4-04) y la ficha (F4-06), cada una con sus textos
// (`texts`: el espacio de mensajes con `owner*` y `branch*`).
export function useContactNames(texts: "contacts" | "contacts.card") {
  const t = useTranslations(texts);
  const { organization, permissions, user } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  const sees = (code: string) => held("contacts.view") && held(code);
  const people = useQuery({
    queryKey: [...getMembersListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => membersList(slug, params, { signal })),
    enabled: sees("users.view"),
    gcTime: 0,
  });
  const branches = useQuery({
    queryKey: [...getBranchesListQueryKey(slug), "all"],
    queryFn: ({ signal }) => everyPage((params) => branchesList(slug, params, { signal })),
    enabled: sees("organization.view"),
    gcTime: 0,
  });
  const names = new Map(
    people.data?.map(({ user: one }) => [
      one.id,
      `${one.first_name} ${one.last_name}`.trim() || one.email,
    ]),
  );
  // Leído aquí, en cada render: TanStack Query solo repinta por lo que se lee, y el contacto
  // puede no estar todavía cuando el directorio termina.
  const naming = people.isLoading;
  const places = new Map(branches.data?.map((row) => [row.id, row]));

  return {
    owner({ owner_user_id: id }: Contact): string {
      if (!id) return t("ownerNone");
      if (id === user.id) return t("ownerYou");
      const name = names.get(id);
      if (!name && naming) return t("ownerPending"); // sin «otro miembro» fugaz
      return name ? t("owner", { name }) : t("ownerOther");
    },
    // `null`: tiene sucursal, pero el usuario no la puede nombrar; no se enseña nada.
    branch({ branch_id: id }: Contact): string | null {
      if (!id) return t("branchNone");
      const place = places.get(id);
      return place ? t("branch", { name: place.name, code: place.code }) : null;
    },
  };
}
