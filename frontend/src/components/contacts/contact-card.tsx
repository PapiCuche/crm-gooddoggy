"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { notFound } from "next/navigation";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { contactsRetrieve, getContactsRetrieveQueryKey } from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { useContactNames } from "./contact-names";

const BOX = "border-border bg-surface flex flex-col gap-4 rounded-lg border p-4";

// La ficha del cliente (F4-06), de solo lectura, con `GET /api/v1/o/{slug}/contacts/{id}/`.
// Solo la pestaña «Resumen»: las demás (notas, etiquetas, oportunidades…) llegan con sus API.
// Fuera del alcance, borrado, de otra organización o inexistente, la API responde el mismo
// 404, y la ficha es la página de «no encontrada» de cualquier dirección: no dice cuál fue.
// Carga, error con «Reintentar», 403 y 401 se tratan como en `CursorList`.
export function ContactCard({ contactId }: { contactId: string }) {
  const t = useTranslations("contacts");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization } = useTenant();
  const slug = organization.slug;
  const { owner, branch } = useContactNames("contacts.card");
  const card = useQuery<Contact, ApiError>({
    queryKey: getContactsRetrieveQueryKey(slug, contactId),
    queryFn: ({ signal }) => contactsRetrieve(slug, contactId, { signal }),
    gcTime: 0, // como la lista: al salir no queda en memoria
  });
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    // La lista y la ficha tienen el mismo título de pestaña, así que Next no anuncia el cambio, y
    // el enlace pulsado ya no existe: el foco va al título, salvo que el usuario esté en otra parte.
    if (document.activeElement === document.body) heading.current?.focus();
  }, []);
  const [retrying, setRetrying] = useState(false);
  // Como la lista: un 403 cierra la ficha aunque ya estuviera en pantalla, y sigue cerrada
  // hasta que la API responde bien (un fallo pasajero posterior no la reabre).
  const status = card.error?.status;
  const [denied, setDenied] = useState(false);
  if (status === 403 && !denied) setDenied(true);
  if (denied && card.isSuccess) setDenied(false);
  if (status === 404) notFound();

  async function retry() {
    setRetrying(true);
    const result = await card.refetch();
    setRetrying(false);
    // La tarjeta se va (llegó la ficha, o la API niega): el foco, al título, si seguía ahí.
    const gone = result.isSuccess || result.error?.status === 403;
    const at = document.activeElement;
    const still = at === document.body || at === retryButton.current;
    if (gone && still) heading.current?.focus();
  }

  const contact = denied ? undefined : card.data;
  let body: ReactNode;
  if (denied) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (contact) {
    const missing = <span className="text-muted">{t("card.missing")}</span>;
    const when = (iso: string) => (
      <time dateTime={iso}>
        {format.dateTime(new Date(iso), { dateStyle: "long", timeStyle: "short" })}
      </time>
    );
    const kind = contact.document_type;
    const place = branch(contact);
    const who = owner(contact);
    // Mientras llega el directorio: «…» a la vista y «Cargando» para el lector de pantalla.
    const waiting = (
      <>
        <span aria-hidden>…</span>
        <span className="sr-only">{who}</span>
      </>
    );
    const fields: [string, ReactNode][] = [
      [t("create.first_name"), contact.first_name || missing],
      [t("create.last_name"), contact.last_name || missing],
      [
        t("phone"),
        contact.primary_phone_e164 ? (
          <span className="font-mono text-[13px]">{contact.primary_phone_e164}</span>
        ) : (
          missing
        ),
      ],
      [t("email"), contact.primary_email || missing],
      [
        t("card.document"),
        kind
          ? `${t(`create.documentType.${kind}`)} ${contact.document_number ?? ""}`.trim()
          : t("create.noDocument"),
      ],
    ];
    const record: [string, ReactNode][] = [
      [t("card.ownerLabel"), who === t("card.ownerPending") ? waiting : who],
      ...(place ? [[t("card.branchLabel"), place] as [string, ReactNode]] : []),
      [t("create.source"), t(`create.sources.${contact.source}`)],
      [t("card.created"), when(contact.created_at)],
      [t("card.updated"), when(contact.updated_at)],
    ];
    const group = (key: "profile" | "record", rows: [string, ReactNode][]) => (
      <section aria-labelledby={`${id}-${key}`} className={BOX}>
        <h2 id={`${id}-${key}`} className="text-lg font-semibold">
          {t(`card.${key}`)}
        </h2>
        <dl className="grid gap-4 sm:grid-cols-2">
          {rows.map(([label, value]) => (
            <div key={label} className="flex min-w-0 flex-col gap-0.5">
              <dt className="text-muted text-sm">{label}</dt>
              <dd className="wrap-anywhere">{value}</dd>
            </div>
          ))}
        </dl>
      </section>
    );
    body = (
      <>
        <div role="tablist" aria-label={t("card.tabs")} className="flex flex-wrap gap-2">
          <button
            type="button"
            role="tab"
            id={`${id}-tab`}
            aria-selected
            aria-controls={`${id}-panel`}
            className="border-foreground/10 bg-accent text-accent-foreground flex h-11 items-center rounded-lg border px-4 text-sm lg:h-9"
          >
            {t("card.summary")}
          </button>
        </div>
        <div
          role="tabpanel"
          tabIndex={0}
          id={`${id}-panel`}
          aria-labelledby={`${id}-tab`}
          className="flex flex-col gap-4"
        >
          {group("profile", fields)}
          {group("record", record)}
        </div>
      </>
    );
  } else if ((card.isError && status !== 401) || retrying) {
    // Sin sesión (401), `Providers` ya lleva al login: queda el aviso de carga, sin error.
    body = (
      <div className="flex flex-col items-start gap-3">
        {card.isError ? (
          <p role="alert" className="text-danger">
            {errors(`api.${apiErrorKey(card.error)}`)}
          </p>
        ) : null}
        <Button
          ref={retryButton}
          variant="primary"
          size="lg"
          aria-disabled={retrying}
          // Ocupado, otra pulsación se une a la petición en vuelo (TanStack no pide dos veces).
          onClick={() => void retry()}
        >
          {retrying ? t("card.loading") : errors("retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("card.loading")}
      </p>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <nav aria-label={t("card.breadcrumb")}>
          <ol className="flex flex-wrap gap-x-1.5 text-[13px] tracking-wide uppercase">
            <li>{t("card.eyebrow")}</li>
            <li>
              <span aria-hidden className="mr-1.5">
                /
              </span>
              <Link href={`/o/${slug}/clientes`} className="underline underline-offset-4">
                {t("title")}
              </Link>
            </li>
          </ol>
        </nav>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] wrap-anywhere focus-visible:outline-none"
        >
          {contact?.display_name ?? t("card.title")}
        </h1>
      </div>
      {body}
    </section>
  );
}
