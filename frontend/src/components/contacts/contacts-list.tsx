"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { contactsList, getContactsListQueryKey } from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";

import { ContactCreate } from "./contact-create";
import { ContactDeleteAction } from "./contact-delete-action";
import { ContactEditAction } from "./contact-edit-action";
import { useContactNames } from "./contact-names";

// Clientes (F4-04): los contactos que devuelve `GET /api/v1/o/{slug}/contacts/` (F4-02), los
// del alcance de `contacts.view`, por orden de creación. La lista, sus estados y su foco son
// los de `CursorList`. El nombre abre la ficha (F4-06); editar y borrar (F4-05), en cada fila.
// Quién puede verlos, crearlos o cambiarlos lo decide la API.
export function ContactsList() {
  const t = useTranslations("contacts");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  const listKey = [...getContactsListQueryKey(slug), "pages"];
  const { owner, branch } = useContactNames("contacts");
  // Lo que respondió la última escritura. Un cliente que ya no está a su alcance (`alert`) se
  // explica aquí y no en su fila, que desaparece cuando llega la lista nueva.
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  const list = useRef<CursorListHandle>(null);
  const ask = () => setSaid(null);
  // Los clientes con el formulario de edición abierto: si la lista nueva ya no los trae (otra
  // pestaña los borró, o salieron de su alcance), su fila sigue con lo escrito y lo dice.
  const [editing, setEditing] = useState<readonly Contact[]>([]);
  const hold = (contact: Contact, open: boolean) =>
    setEditing((now) => [...now.filter(({ id }) => id !== contact.id), ...(open ? [contact] : [])]);
  function stale(name: string, here: boolean) {
    setSaid({ text: t("stale", { name }), alert: true });
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  function done(text: string, here = false) {
    setSaid({ text });
    if (here) list.current?.focusHeading(); // borrado: la fila se fue y el foco no tiene dónde
  }
  const actions = { slug, listKey, onAsk: ask, onStale: stale };
  // Comodidad: cada escritura se ofrece a quien la API dijo que tiene su permiso, con cualquier
  // alcance; si un cliente concreto queda fuera, la API responde 404 y se dice.
  const canEdit = held("contacts.update");
  const canDelete = held("contacts.delete");
  const writes = canEdit || canDelete || held("contacts.create");

  return (
    <CursorList<Contact>
      ref={list}
      section="contacts"
      organization={organization.name}
      listKey={listKey}
      keep={editing}
      fetchPage={(cursor, signal) =>
        contactsList(slug, cursor ? { cursor } : undefined, { signal })
      }
      notice={
        writes ? (
          <>
            {held("contacts.create") ? (
              <ContactCreate slug={slug} listKey={listKey} onAsk={ask} onDone={done} />
            ) : null}
            {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
            <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
              {said && !said.alert ? said.text : ""}
            </p>
            {said?.alert ? (
              <p role="alert" className="text-danger wrap-anywhere">
                {said.text}
              </p>
            ) : null}
          </>
        ) : null
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(contact, rows) => {
        const place = branch(contact);
        return (
          <>
            <div className="flex min-w-0 flex-col leading-snug">
              {/* El nombre abre la ficha (F4-06). */}
              <Link
                href={`/o/${slug}/clientes/${contact.id}`}
                prefetch={false}
                className="self-start font-medium wrap-anywhere underline-offset-4 hover:underline"
              >
                {contact.display_name}
              </Link>
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
            {canEdit || canDelete ? (
              // Abiertos, el formulario o la confirmación ocupan la fila entera.
              <div className="flex flex-wrap items-start gap-2 sm:col-span-2">
                {canEdit ? (
                  <ContactEditAction
                    {...actions}
                    contact={contact}
                    gone={!rows.some(({ id }) => id === contact.id)}
                    onHold={hold}
                    onDone={done}
                  />
                ) : null}
                {canDelete ? (
                  <ContactDeleteAction {...actions} contact={contact} onDeleted={done} />
                ) : null}
              </div>
            ) : null}
          </>
        );
      }}
    </CursorList>
  );
}
