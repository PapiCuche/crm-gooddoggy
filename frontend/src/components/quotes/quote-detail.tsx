"use client";

import { useQuery } from "@tanstack/react-query";
import { Clock } from "lucide-react";
import Link from "next/link";
import { notFound } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Plain } from "@/components/inbox/plain";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import { getQuotesRetrieveQueryKey, quotesRetrieve } from "@/lib/api/client";
import type { QuoteDetail as Detail } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { LineDiscount } from "./line-discount";
import { QuoteActions } from "./quote-actions";
import { QuoteEditor } from "./quote-editor";
import { isLate, QuoteStatus, ratePercent, useDay, useHeld, useMoney } from "./quote-parts";

const BOX = "border-border bg-surface flex flex-col gap-3 rounded-lg border p-4";
const COLUMNS = ["product", "quantity", "list", "promotion", "discount", "final", "total"] as const;

// El detalle de una cotización (F5-09f), con `GET …/quotes/{id}/` (F5-09d2): cabecera, datos, las
// líneas tal como se congelaron al cotizar, los totales con el IGV y las notas. Es solo lectura.
// Fuera del alcance, de otra organización o inexistente la API da el mismo 404 y la pantalla es la
// página de «no encontrada». El nombre del cliente llega `null` sin `contacts.view`: se dice sin
// enlace. Todo lo que escribió otra persona (cliente, producto, notas, motivo) va por `Plain`. Los
// importes solo se formatean; ningún costo ni margen: la API no los da. Un borrador se edita y se
// descuenta por línea (F5-09g) y se pide, decide o retira su aprobación (F5-09h); se emite, se
// registra la respuesta del cliente, se revisa o se cancela (F5-09i), según los permisos de la
// sesión (la API los vuelve a comprobar); mientras espera aprobación, un aviso fijo lo dice. Lo
// congelado al emitir y el motivo del rechazo del cliente se muestran como el de la cancelación.
export function QuoteDetail({ quoteId }: { quoteId: string }) {
  const t = useTranslations("quotes.detail");
  const slug = useTenant().organization.slug;
  const held = useHeld();
  const [editing, setEditing] = useState(false);
  const [discounting, setDiscounting] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const { failed, retry } = useReads();
  const money = useMoney();
  const day = useDay();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Una pestaña que se deja abierta pasa de día: al volver a ella, «vencida» se calcula con la
    // hora de ahora.
    const refresh = () => document.visibilityState !== "hidden" && setNow(Date.now());
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
  const read = useQuery<Detail, ApiError>({
    queryKey: getQuotesRetrieveQueryKey(slug, quoteId),
    queryFn: ({ signal }) => quotesRetrieve(slug, quoteId, { signal }),
    gcTime: 0, // al salir no queda en memoria: cada visita pregunta a la API
  });
  if (read.error?.status === 404) notFound();
  const quote = read.data;
  const problem = failed(read, t("denied"));
  const back = (
    <Link href={`/o/${slug}/cotizaciones`} className="text-sm underline-offset-4 hover:underline">
      ‹ {t("back")}
    </Link>
  );
  if (!quote) {
    return (
      <section className="flex flex-col items-start gap-4">
        {back}
        {problem ? (
          <>
            <p role="alert" className={read.error?.status === 403 ? undefined : "text-danger"}>
              {problem}
            </p>
            {retry(read)}
          </>
        ) : (
          <p role="status" className="text-muted">
            {t("loading")}
          </p>
        )}
      </section>
    );
  }
  const cur = quote.currency;
  const late = isLate(quote, now);
  const editable = quote.status === "DRAFT" && held("quotes.update", quote);
  const discountable = editable && held("quotes.discount", quote);
  const target = quote.items.find((x) => x.id === discounting);
  const facts: [string, ReactNode][] = [
    [
      t("contact"),
      quote.contact.display_name === null ? (
        <span className="text-muted">{t("noContact")}</span>
      ) : (
        <Link
          href={`/o/${slug}/clientes/${quote.contact.id}`}
          prefetch={false}
          className="underline-offset-4 hover:underline"
        >
          <Plain>{quote.contact.display_name}</Plain>
        </Link>
      ),
    ],
    ...(quote.opportunity_id
      ? [
          [
            t("opportunity"),
            <Link
              key="op"
              href={`/o/${slug}/oportunidades/${quote.opportunity_id}`}
              prefetch={false}
              className="underline-offset-4 hover:underline"
            >
              {t("viewOpportunity")}
            </Link>,
          ] as [string, ReactNode],
        ]
      : []),
    [
      t("validUntil"),
      quote.valid_until ? (
        <span className={late ? "text-danger font-medium" : undefined}>
          {day(quote.valid_until)}
          {late ? ` · ${t("late")}` : ""}
        </span>
      ) : (
        t("none")
      ),
    ],
    [t("created"), day(quote.created_at)],
    // La emisión y la respuesta del cliente (F5-09i), cuando las hay.
    ...(["sent", "accepted", "rejected"] as const).flatMap((key) => {
      const at = quote[`${key}_at`];
      return at ? [[t(key), day(at)] as [string, ReactNode]] : [];
    }),
    [t("currency"), cur],
    // Según `tax_mode`: con IGV incluido los precios ya lo llevan; si no, se suma aparte. Un modo que
    // esta versión no conoce dice solo la tasa.
    [
      t("tax"),
      t(
        quote.tax_mode === "INCLUDED"
          ? "taxIncluded"
          : quote.tax_mode === "EXCLUDED"
            ? "taxExcluded"
            : "taxRate",
        { rate: ratePercent(quote.tax_rate) },
      ),
    ],
  ];
  const totals: [string, string | undefined, boolean][] = [
    [t("totals.subtotal"), quote.subtotal, false],
    [t(quote.tax_mode === "INCLUDED" ? "totals.tax" : "totals.taxPlain"), quote.tax_total, false],
    [t("totals.discount"), quote.discount_total, false],
    [t("totals.total"), quote.total, true],
  ];
  const notes = [
    ["customer", quote.notes_to_customer],
    ["internal", quote.internal_notes],
    ["terms", quote.terms_snapshot],
    ["warranty", quote.warranty_snapshot],
    ["rejection", quote.rejection_reason],
    ["cancel", quote.cancel_reason],
  ] as const;
  return (
    <section className="flex max-w-5xl flex-col gap-6">
      {back}
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <h1
          tabIndex={-1}
          data-focus-home
          className="flex flex-col text-[28px] leading-tight font-bold tracking-[0.02em] outline-none"
        >
          <span className="font-mono">{quote.number}</span>
          {quote.revision && quote.revision > 1 ? (
            <span className="text-muted text-base font-normal">
              {t("revision", { number: quote.revision })}
            </span>
          ) : null}
        </h1>
        <div className="flex flex-wrap items-center gap-3">
          <QuoteStatus status={quote.status} late={late} />
          {editable ? (
            <Button
              variant="ghost"
              className="border-border min-h-11 border sm:min-h-9"
              onClick={() => {
                setSaid("");
                setDiscounting(null);
                setEditing(true);
              }}
            >
              {t("edit")}
            </Button>
          ) : null}
        </div>
      </div>
      {quote.status === "PENDING_APPROVAL" ? (
        <aside
          aria-label={t("waiting.title")}
          className="border-border bg-surface-raised flex gap-3 rounded-lg border p-4"
        >
          <Clock aria-hidden className="text-muted mt-0.5 size-5 shrink-0" />
          <div className="flex flex-col gap-1">
            <p className="font-medium">{t("waiting.title")}</p>
            <p className="text-muted text-sm">
              {t(quote.approval_asked_by_me ? "waiting.mine" : "waiting.other")}
            </p>
          </div>
        </aside>
      ) : null}
      {editing ? null : <QuoteActions quote={quote} late={late} onDone={setSaid} />}
      <p role="status" className={said ? "text-sm" : "sr-only"}>
        {said}
      </p>
      {editing ? (
        <QuoteEditor
          quote={quote}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            setSaid(t("saved"));
          }}
        />
      ) : null}
      <dl
        aria-label={t("facts")}
        className={`${BOX} sm:grid sm:grid-cols-[auto_minmax(0,1fr)] sm:gap-x-6`}
      >
        {facts.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted text-sm">{label}</dt>
            <dd className="wrap-anywhere">{value}</dd>
          </div>
        ))}
      </dl>
      <section className="flex flex-col gap-3" aria-label={t("items.title")}>
        <h2 className="text-lg font-semibold">{t("items.title")}</h2>
        {quote.items.length === 0 ? (
          <p className="text-muted">{t("items.empty")}</p>
        ) : (
          <div role="region" aria-label={t("items.label")} tabIndex={0} className="overflow-x-auto">
            <table className="bg-surface border-border w-full min-w-[52rem] border text-left text-sm">
              <thead className="border-border border-b">
                <tr>
                  {COLUMNS.map((key) => (
                    <th key={key} scope="col" className="px-3 py-2 font-medium">
                      {t(`items.${key}`)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {quote.items.map((line) => (
                  <tr key={line.id} className="border-border border-b align-top last:border-b-0">
                    <td className="px-3 py-2 wrap-anywhere">
                      <Plain>{line.product_name}</Plain>
                      <span className="text-muted block">
                        <Plain>{line.variant_name}</Plain> ·{" "}
                        <span className="font-mono text-[13px]">
                          <Plain>{line.sku}</Plain>
                        </span>
                      </span>
                    </td>
                    <td className="px-3 py-2">{line.quantity}</td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {money(line.list_unit_price, line.currency)}
                    </td>
                    <td className="px-3 py-2 wrap-anywhere">
                      {line.promotion_name ? <Plain>{line.promotion_name}</Plain> : t("none")}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {line.discount_amount && Number(line.discount_amount) > 0
                        ? money(line.discount_amount, line.currency)
                        : t("none")}
                      {discountable && !editing ? (
                        <Button
                          variant="ghost"
                          className="border-border mt-1 flex min-h-11 border sm:min-h-9"
                          aria-label={t("discountLabel", { product: line.product_name })}
                          onClick={() => {
                            setSaid("");
                            setDiscounting(line.id);
                          }}
                        >
                          {t("discount")}
                        </Button>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {money(line.final_unit_price, line.currency)}
                    </td>
                    <td className="px-3 py-2 font-medium whitespace-nowrap">
                      {money(line.line_total, line.currency)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {target ? (
          <LineDiscount
            key={target.id}
            quote={quote}
            line={target}
            onClose={(text) => {
              setDiscounting(null);
              if (text) setSaid(text);
            }}
          />
        ) : null}
      </section>
      <section className={`${BOX} sm:max-w-sm`} aria-label={t("totals.title")}>
        <h2 className="font-medium">{t("totals.title")}</h2>
        <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1">
          {totals.map(([label, amount, strong]) => (
            <div
              key={label}
              className={strong ? "border-border contents font-semibold" : "contents"}
            >
              <dt className={strong ? undefined : "text-muted"}>{label}</dt>
              <dd className="text-right">{money(amount, cur)}</dd>
            </div>
          ))}
        </dl>
      </section>
      {notes.map(([key, text]) =>
        text ? (
          <section key={key} className={BOX} aria-label={t(`notes.${key}`)}>
            <h2 className="font-medium">{t(`notes.${key}`)}</h2>
            <p>
              <Plain>{text}</Plain>
            </p>
            {key === "cancel" && quote.cancelled_at ? (
              <p className="text-muted text-sm">
                {t("notes.cancelledAt", { date: day(quote.cancelled_at) })}
              </p>
            ) : null}
          </section>
        ) : null,
      )}
    </section>
  );
}
