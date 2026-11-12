"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { Plain } from "@/components/inbox/plain";
import { CursorList } from "@/components/lists/cursor-list";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { getQuotesListQueryKey, quotesList } from "@/lib/api/client";
import { QuoteStatusEnum, type Quote, type QuotesListParams } from "@/lib/api/model";

import { SEARCH_DELAY } from "../contacts/contact-search";
import { isLate, QuoteStatus, useDay, useMoney } from "./quote-parts";

const STATUSES = Object.values(QuoteStatusEnum);

// Las cotizaciones (F5-09f) de `GET /api/v1/o/{slug}/quotes/` (F5-09d), por cursor: filtros de
// estado, «las que creé», vencidas, número exacto y, si la dirección lo trae, un cliente
// (`?cliente=`). Quién ve qué lo decide la API (`quotes.view` y su alcance): un 403 solo se
// explica. Es solo lectura. Nombres y números van como texto plano. Ningún costo: la API no lo da.
export function QuotesList({ contactId }: { contactId?: string }) {
  const t = useTranslations("quotes");
  const { organization } = useTenant();
  const slug = organization.slug;
  const money = useMoney();
  const day = useDay();
  const [now] = useState(() => Date.now());
  const [status, setStatus] = useState("");
  const [mine, setMine] = useState(false);
  const [expired, setExpired] = useState(false);
  const [draft, setDraft] = useState("");
  const [number, setNumber] = useState("");
  const [contact, setContact] = useState(contactId);
  useEffect(() => {
    const timer = setTimeout(() => setNumber(draft.trim()), SEARCH_DELAY);
    return () => clearTimeout(timer);
  }, [draft]);
  const params: QuotesListParams = {
    ...(STATUSES.some((one) => one === status)
      ? { status: status as QuotesListParams["status"] }
      : {}),
    ...(mine ? { mine: true } : {}),
    ...(expired ? { expired: true } : {}),
    ...(number ? { number } : {}),
    ...(contact ? { contact_id: contact } : {}),
  };
  const filtered = Object.keys(params).length > 0;

  return (
    <CursorList<Quote>
      section="quotes"
      organization={organization.name}
      listKey={[...getQuotesListQueryKey(slug, params), "pages"]}
      fetchPage={(cursor, signal) =>
        quotesList(slug, { ...params, ...(cursor ? { cursor } : {}) }, { signal })
      }
      controls={
        <div role="search" aria-label={t("filters.label")} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Choice
              label={t("filters.status")}
              value={status}
              busy={false}
              onChange={setStatus}
              options={[
                ["", t("filters.anyStatus")],
                ...STATUSES.map((one): [string, string] => [one, t(`statuses.${one}`)]),
              ]}
            />
            <TextField
              type="search"
              label={t("filters.number")}
              hint={t("filters.numberHint")}
              value={draft}
              autoComplete="off"
              onChange={(event) => setDraft(event.target.value)}
            />
          </div>
          <div className="flex flex-wrap gap-x-6">
            <label className="flex min-h-11 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={mine}
                onChange={(event) => setMine(event.target.checked)}
              />
              {t("filters.mine")}
            </label>
            <label className="flex min-h-11 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={expired}
                onChange={(event) => setExpired(event.target.checked)}
              />
              {t("filters.expired")}
            </label>
          </div>
          {contact ? (
            <p className="flex flex-wrap items-center gap-3 text-sm">
              {t("filters.contact")}
              <Button
                variant="ghost"
                className="border-border min-h-11 border sm:min-h-9"
                onClick={() => setContact(undefined)}
              >
                {t("filters.clearContact")}
              </Button>
            </p>
          ) : null}
        </div>
      }
      empty={<p className="text-muted">{t(filtered ? "empty" : "emptyAll")}</p>}
      rowClassName="flex flex-col gap-2"
    >
      {(quote) => (
        <>
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-1">
            <p className="flex min-w-0 flex-col leading-snug">
              <Link
                href={`/o/${slug}/cotizaciones/${quote.id}`}
                prefetch={false}
                className="font-mono font-medium underline-offset-4 hover:underline"
              >
                <Plain>{quote.number}</Plain>
              </Link>
              {quote.revision && quote.revision > 1 ? (
                <span className="text-muted text-sm">
                  {t("row.revision", { number: quote.revision })}
                </span>
              ) : null}
            </p>
            <QuoteStatus status={quote.status} late={isLate(quote, now)} />
          </div>
          <p className="wrap-anywhere">
            {quote.contact.display_name === null ? (
              <span className="text-muted">{t("row.noContact")}</span>
            ) : (
              <Plain>{quote.contact.display_name}</Plain>
            )}
          </p>
          <p className="text-lg font-semibold">{money(quote.total, quote.currency)}</p>
          <p className="text-muted flex flex-wrap gap-x-4 gap-y-1 text-sm">
            <span>{t("row.created", { date: day(quote.created_at) })}</span>
            <span className={isLate(quote, now) ? "text-danger font-medium" : undefined}>
              {quote.valid_until
                ? t("row.validUntil", { date: day(quote.valid_until) })
                : t("row.noValidity")}
              {isLate(quote, now) ? ` · ${t("row.late")}` : ""}
            </span>
          </p>
        </>
      )}
    </CursorList>
  );
}
