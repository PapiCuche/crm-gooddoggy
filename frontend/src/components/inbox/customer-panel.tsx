"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, type Ref } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CustomerOpportunities } from "@/components/deals/customer-opportunities";
import { Button } from "@/components/ui/button";
import { contactsRetrieve, getContactsRetrieveQueryKey } from "@/lib/api/client";
import type { Contact, ConversationContact } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { Plain } from "./plain";

const BOX = "border-border bg-surface flex flex-col gap-3 rounded-lg border p-4";

// El panel del cliente de una conversación (F6-06): su resumen (`GET …/contacts/{id}/`, el de la
// ficha 360°) y sus oportunidades con crear una nueva (la pestaña de la ficha, reutilizada), todo
// sin salir del Inbox. Solo se abre si la API dio el nombre del contacto (D-F6-10): sin
// `contacts.view` sobre él no se pide ni se muestra nada suyo. Quién ve qué lo decide la API: un
// 403 o un 404 solo se explican. Los textos de la persona (nombre, teléfono, correo, etiquetas) van
// por `Plain`.
export function CustomerPanel({
  contact,
  headingRef,
  onBack,
}: {
  contact: ConversationContact;
  headingRef: Ref<HTMLHeadingElement>;
  onBack: () => void;
}) {
  const t = useTranslations("inbox.customer");
  const errors = useTranslations("errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  // La fila de la lista es una instantánea: si ya no trae el nombre (D-F6-10), aquí tampoco se pide
  // nada del contacto, aunque se haya llegado hasta el panel.
  const named = contact.display_name !== null;
  const name = contact.display_name ?? "";
  const card = useQuery<Contact, ApiError>({
    queryKey: getContactsRetrieveQueryKey(slug, contact.id),
    queryFn: ({ signal }) => contactsRetrieve(slug, contact.id, { signal }),
    enabled: named && held("contacts.view"),
    gcTime: 0, // como la ficha: al salir no queda en memoria
  });
  const status = card.error?.status;

  let summary: ReactNode;
  if (!named || !held("contacts.view") || status === 403 || status === 404) {
    summary = <p className="text-muted">{t("unavailable")}</p>;
  } else if (card.data) {
    const missing = <span className="text-muted">{t("missing")}</span>;
    const { primary_phone_e164: phone, primary_email: email, tags } = card.data;
    const rows: [string, ReactNode][] = [
      [t("phone"), phone ? <span className="font-mono text-[13px]">{phone}</span> : missing],
      [t("email"), email ? <Plain>{email}</Plain> : missing],
      [
        t("tags"),
        tags.length ? tags.map((tag) => <Plain key={tag.id}>{`${tag.name} `}</Plain>) : missing,
      ],
    ];
    summary = (
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="wrap-anywhere">{value}</dd>
          </div>
        ))}
      </dl>
    );
  } else if (card.isError) {
    summary = (
      <div className="flex flex-col items-start gap-2">
        <p role="alert" className="text-danger text-sm">
          {errors(`api.${apiErrorKey(card.error)}`)}
        </p>
        <Button
          variant="ghost"
          className="border-border border"
          onClick={() => void card.refetch()}
        >
          {errors("retry")}
        </Button>
      </div>
    );
  } else {
    summary = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <Button
        variant="ghost"
        className="border-border min-h-11 self-start border sm:min-h-9"
        onClick={onBack}
      >
        <ArrowLeft aria-hidden />
        {t("back")}
      </Button>
      <section className={BOX} aria-label={t("title")}>
        <h2
          ref={headingRef}
          tabIndex={-1}
          className="text-lg font-semibold wrap-anywhere focus-visible:outline-none"
        >
          {named ? <Plain>{name}</Plain> : t("title")}
        </h2>
        {summary}
        <Link
          href={`/o/${slug}/clientes/${contact.id}`}
          prefetch={false}
          className="self-start text-sm font-medium underline underline-offset-4"
        >
          {t("card")}
        </Link>
      </section>
      {named && held("opportunities.view") ? (
        <CustomerOpportunities contactId={contact.id} contactName={name} />
      ) : (
        <p className="text-muted text-sm">{t("noDeals")}</p>
      )}
    </div>
  );
}
