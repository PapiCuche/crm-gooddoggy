"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type Ref, useEffect, useId, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { TextField } from "@/components/ui/text-field";
import { contactsList, getContactsListQueryKey } from "@/lib/api/client";

import { SEARCH_DELAY, searchTerm } from "../contacts/contact-search";

export type Customer = { id: string; name: string };

// Elegir el cliente de una oportunidad: busca en `GET …/contacts/` (el alcance de `contacts.view`,
// el mismo que exige la API al crear) con la misma búsqueda de Clientes, y ofrece los primeros
// resultados como opciones de un grupo. Sin `contacts.view` no hay nada que ofrecer y se dice.
// Lo elegido se conserva aunque la búsqueda siguiente ya no lo traiga.
export function CustomerPicker({
  slug,
  picked,
  onPick,
  error,
  searchRef,
}: {
  slug: string;
  picked: Customer | null;
  onPick: (customer: Customer) => void;
  error?: string;
  searchRef?: Ref<HTMLInputElement>; // para llevar el foco a la búsqueda si falta el cliente
}) {
  const t = useTranslations("deals.create.customer");
  const errorId = useId();
  const { permissions } = useTenant();
  const can = permissions.some((grant) => grant.code === "contacts.view");
  const { failed, retry } = useReads();
  const [draft, setDraft] = useState("");
  const [term, setTerm] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setTerm(searchTerm(draft)), SEARCH_DELAY);
    return () => clearTimeout(timer);
  }, [draft]);
  const params = { limit: 8, ...(term ? { q: term } : {}) };
  const found = useQuery({
    queryKey: [...getContactsListQueryKey(slug, params), "picker"],
    queryFn: ({ signal }) => contactsList(slug, params, { signal }),
    enabled: can,
    gcTime: 0,
  });
  const problem = failed(found, t("denied"));
  return (
    <fieldset className="flex flex-col gap-2" aria-describedby={error ? errorId : undefined}>
      <legend className="text-sm font-medium">{t("label")}</legend>
      {can ? (
        <>
          <TextField
            ref={searchRef}
            type="search"
            label={t("search")}
            hint={t("hint")}
            value={draft}
            autoComplete="off"
            onChange={(event) => setDraft(event.target.value)}
            // Enter busca (la espera ya lo hace): no envía el formulario de la oportunidad.
            onKeyDown={(event) => event.key === "Enter" && event.preventDefault()}
          />
          {problem ? (
            <p role="alert" className="text-danger text-sm">
              {problem}
            </p>
          ) : found.isPending ? (
            <p role="status" className="text-muted text-sm">
              {t("loading")}
            </p>
          ) : found.data?.results.length === 0 ? (
            <p className="text-muted text-sm">{t("none")}</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {found.data?.results.map((contact) => (
                <li key={contact.id}>
                  <label className="flex min-h-11 items-center gap-2 sm:min-h-9">
                    <input
                      type="radio"
                      name="customer"
                      checked={picked?.id === contact.id}
                      onChange={() => onPick({ id: contact.id, name: contact.display_name })}
                    />
                    <span className="wrap-anywhere">{contact.display_name}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          {retry(found)}
        </>
      ) : (
        <p className="text-muted text-sm">{t("denied")}</p>
      )}
      {/* Montado desde el principio: un lector de pantalla anuncia al cliente elegido. */}
      <p role="status" className={picked ? "text-sm font-medium" : "sr-only"}>
        {picked ? t("picked", { name: picked.name }) : ""}
      </p>
      {error ? (
        <p id={errorId} role="alert" className="text-danger text-sm">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}
