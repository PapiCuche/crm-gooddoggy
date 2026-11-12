"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Plain } from "@/components/inbox/plain";
import { useReads } from "@/components/inventory/cell-picker";
import { getQuotesListQueryKey, quotesList } from "@/lib/api/client";
import type { PaginatedQuoteList } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { isLate, QuoteStatus, useDay, useMoney } from "./quote-parts";

const SHOWN = 50;
// Como la ruta de la lista: solo un UUID es un cliente que se puede pasar a `?cliente=`.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// Las cotizaciones de un cliente o de una oportunidad (F5-09f3): la pestaña «Cotizaciones» de la
// ficha y la sección del detalle de la oportunidad, con `GET …/quotes/?contact_id=` u
// `?opportunity_id=` (F5-09d). Solo lectura. Lo que se ve lo decide la API (`quotes.view` y su
// alcance); un 403 solo se explica. Si hay más de las que caben se dice y «Ver todas» abre la lista
// de ese cliente (`contactId`). Los importes solo se formatean; los números van como texto plano.
export function LinkedQuotes({
  link,
  contactId,
}: {
  link: { contact_id: string } | { opportunity_id: string };
  contactId: string;
}) {
  const t = useTranslations("quotes.linked");
  const slug = useTenant().organization.slug;
  const { failed, retry } = useReads();
  const money = useMoney();
  const day = useDay();
  const [now] = useState(() => Date.now());
  const params = { ...link, limit: SHOWN };
  const read = useQuery<PaginatedQuoteList, ApiError>({
    queryKey: getQuotesListQueryKey(slug, params),
    queryFn: ({ signal }) => quotesList(slug, params, { signal }),
    gcTime: 0, // al salir no queda en memoria: cada visita pregunta a la API
  });
  const problem = failed(read, t("denied"));
  const rows = read.data?.results.length ?? 0;
  let body;
  if (read.data) {
    const results = read.data.results;
    body =
      results.length === 0 ? (
        <p className="text-muted">{t("empty")}</p>
      ) : (
        <ul aria-label={t("title")} className="flex flex-col gap-2">
          {results.map((quote) => (
            <li
              key={quote.id}
              className="border-border bg-surface flex flex-col gap-1 rounded-lg border p-4"
            >
              <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
                <Link
                  href={`/o/${slug}/cotizaciones/${quote.id}`}
                  prefetch={false}
                  className="font-mono font-medium underline-offset-4 hover:underline"
                >
                  <Plain>{quote.number}</Plain>
                </Link>
                <QuoteStatus status={quote.status} late={isLate(quote, now)} />
              </div>
              <p className="font-semibold">{money(quote.total, quote.currency)}</p>
              <p className="text-muted text-sm">
                {quote.valid_until
                  ? t("validUntil", { date: day(quote.valid_until) })
                  : t("noValidity")}
              </p>
            </li>
          ))}
        </ul>
      );
  } else if (problem) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className={read.error?.status === 403 ? undefined : "text-danger"}>
          {problem}
        </p>
        {retry(read)}
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">{t("title")}</h2>
      {body}
      {read.data && (read.data.next !== null || (rows > 0 && UUID.test(contactId))) ? (
        <p className="text-sm">
          {read.data.next !== null ? `${t("partial", { count: SHOWN })} ` : ""}
          {UUID.test(contactId) ? (
            <Link
              href={`/o/${slug}/cotizaciones?cliente=${encodeURIComponent(contactId)}`}
              prefetch={false}
              className="underline-offset-4 hover:underline"
            >
              {t("viewAll")}
            </Link>
          ) : null}
        </p>
      ) : null}
    </section>
  );
}
