"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { everyPage } from "@/components/catalog/categories-tree";
import { CursorList } from "@/components/lists/cursor-list";
import {
  branchesList,
  contactsList,
  getBranchesListQueryKey,
  getContactsListQueryKey,
  getMembersListQueryKey,
  membersList,
} from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";

import { ContactCreate } from "./contact-create";

// Clientes (F4-04): los contactos que devuelve `GET /api/v1/o/{slug}/contacts/` (F4-02), los
// del alcance de `contacts.view`, por orden de creación. La lista, sus estados y su foco son
// los de `CursorList`. Quién puede verlos o crearlos lo decide la API.
export function ContactsList() {
  const t = useTranslations("contacts");
  const { organization, permissions, user } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  const listKey = [...getContactsListQueryKey(slug), "pages"];
  // El responsable y la sucursal llegan como identificadores. Se nombran con el directorio y
  // con las sucursales solo si la API dijo que el usuario puede leerlos (`users.view`,
  // `organization.view`) y además ve la lista; si no, o si fallan, funciona con menos detalle.
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
  // Leído aquí, en cada render: TanStack Query solo repinta por lo que se lee, y las filas
  // pueden no estar todavía cuando el directorio termina.
  const naming = people.isLoading;
  const places = new Map(branches.data?.map((row) => [row.id, row]));
  const [done, setDone] = useState<string | null>(null);

  function owner({ owner_user_id: id }: Contact): string {
    if (!id) return t("ownerNone");
    if (id === user.id) return t("ownerYou");
    const name = names.get(id);
    if (!name && naming) return t("ownerPending"); // sin «otro miembro» fugaz
    return name ? t("owner", { name }) : t("ownerOther");
  }
  function branch({ branch_id: id }: Contact): string | null {
    if (!id) return t("branchNone");
    const place = places.get(id);
    return place ? t("branch", { name: place.name, code: place.code }) : null;
  }

  return (
    <CursorList<Contact>
      section="contacts"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) =>
        contactsList(slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        // Comodidad: crear se ofrece a quien la API dijo que tiene `contacts.create`.
        held("contacts.create") ? (
          <>
            <ContactCreate
              slug={slug}
              listKey={listKey}
              onAsk={() => setDone(null)}
              onDone={setDone}
            />
            {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
            <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
              {done ?? ""}
            </p>
          </>
        ) : null
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(contact) => {
        const place = branch(contact);
        return (
          <>
            <div className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium wrap-anywhere">{contact.display_name}</span>
              {contact.primary_phone_e164 ? (
                <span className="text-muted text-sm">
                  <span className="sr-only">{t("phone")}: </span>
                  <span className="font-mono text-[13px]">{contact.primary_phone_e164}</span>
                </span>
              ) : null}
              {contact.primary_email ? (
                <span className="text-muted text-sm wrap-anywhere">
                  <span className="sr-only">{t("email")}: </span>
                  {contact.primary_email}
                </span>
              ) : null}
            </div>
            <p className="text-muted flex flex-wrap gap-x-3 text-sm sm:flex-col sm:items-end">
              <span className="wrap-anywhere">{owner(contact)}</span>
              {place ? <span className="wrap-anywhere">{place}</span> : null}
            </p>
          </>
        );
      }}
    </CursorList>
  );
}
