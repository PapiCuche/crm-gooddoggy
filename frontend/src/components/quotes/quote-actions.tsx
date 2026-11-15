"use client";

import { useRouter } from "next/navigation";
import { useTimeZone, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { localValue, ZONE, zonedInstant } from "@/components/pricing/zoned";
import { Button } from "@/components/ui/button";
import {
  quotesAccept,
  quotesApprove,
  quotesCancel,
  quotesDecline,
  quotesIssue,
  quotesReject,
  quotesRequestApproval,
  quotesRevise,
  quotesWithdrawRequest,
} from "@/lib/api/client";
import type { QuoteDetail } from "@/lib/api/model";

import {
  DAYS,
  Field,
  FIELD,
  focusHome,
  useFocusTrip,
  useQuoteProblem,
  useQuoteWrite,
} from "./quote-editor";
import { useHeld } from "./quote-parts";

type Asked = "approve" | "reject" | "issue" | "accept" | "decline" | "revise" | "cancel";
type Verb = Asked | "request" | "withdraw";
type Sent = Partial<Record<"comment" | "reason" | "terms" | "warranty" | "valid_until", string>>;
// Lo que se escribe en cada formulario: su campo en la API, su tope (el de la API) y si es
// obligatorio. Aceptar y revisar solo piden confirmación.
const TEXTS: Record<Asked, { name: keyof Sent; max: number; needed?: boolean }[]> = {
  approve: [{ name: "comment", max: 500 }],
  reject: [{ name: "reason", max: 200, needed: true }],
  issue: [
    { name: "terms", max: 2000 },
    { name: "warranty", max: 500 },
  ],
  accept: [],
  decline: [{ name: "reason", max: 500, needed: true }],
  revise: [],
  cancel: [{ name: "reason", max: 500 }],
};
// Todas reciben la `version` y, según la acción, sus textos opcionales.
type Call = (slug: string, id: string, body: { version: number } & Sent) => Promise<QuoteDetail>;
const CALLS: Record<Verb, Call> = {
  request: quotesRequestApproval,
  withdraw: quotesWithdrawRequest,
  approve: quotesApprove,
  reject: quotesReject as Call,
  issue: quotesIssue,
  accept: quotesAccept,
  decline: quotesDecline as Call,
  revise: quotesRevise,
  cancel: quotesCancel,
};
const FORWARD: Verb[] = ["request", "approve", "issue", "accept"];
const OPEN = ["DRAFT", "APPROVED", "SENT", "VIEWED"];

// Las acciones de una cotización (F5-09h y F5-09i), cada una con su estado y su permiso (la API los
// vuelve a comprobar): validar un borrador, que queda aprobado si cabe en el límite o espera
// aprobación; retirar la solicitud (quien la pidió o quien aprueba); aprobarla o rechazarla (quien
// aprueba, nunca quien la pidió); emitir con validez, condiciones y garantía; registrar que el
// cliente aceptó o rechazó (con su motivo) mientras sigue vigente; abrir una revisión, que lleva a
// ella; y cancelar. Lo que no se deshace pide confirmación. Se manda la `version` vista al abrir; un
// 409 vuelve a leer el detalle y se explica por su código.
export function QuoteActions({
  quote,
  late,
  onDone,
}: {
  quote: QuoteDetail;
  late: boolean;
  onDone: (said: string) => void;
}) {
  const t = useTranslations("quotes.actions");
  const slug = useTenant().organization.slug;
  const held = useHeld();
  const problemOf = useQuoteProblem();
  const [open, setOpen] = useState<Asked | null>(null);
  const [revised, setRevised] = useState<{ from: string; to: string } | null>(null);
  const job = useRef<{ verb: Verb; version: number; sent: Sent }>(null);
  const moved = useRef(false);
  useEffect(() => {
    // Tras una acción, su botón puede ya no ofrecerse: el foco no se pierde.
    if (moved.current) focusHome();
    moved.current = false;
  });
  const run = useQuoteWrite(
    quote.id,
    () => {
      const { verb, version, sent } = job.current!;
      return CALLS[verb](slug, quote.id, { version, ...sent });
    },
    (saved) => {
      const verb = job.current!.verb;
      moved.current = true;
      setOpen(null);
      if (verb === "revise") setRevised({ from: quote.id, to: saved.id });
      const now = verb === "request" && saved.status === "APPROVED";
      onDone(t(now ? "done.approvedNow" : `done.${verb}`, { number: saved.revision ?? 1 }));
    },
  );
  const go = (verb: Verb, version = quote.version ?? 1, sent: Sent = {}) => {
    if (run.isPending) return;
    job.current = { verb, version, sent };
    onDone("");
    run.mutate();
  };
  // La revisión nueva ya existe: se va a ella (solo desde la cotización que se revisó).
  if (revised?.from === quote.id) return <GoTo href={`/o/${slug}/cotizaciones/${revised.to}`} />;

  const status = quote.status;
  const pending = status === "PENDING_APPROVAL";
  const answerable = (status === "SENT" || status === "VIEWED") && !late;
  const can = (...codes: string[]) => codes.every((code) => held(code, quote));
  const decides = pending && can("quotes.approve") && !quote.approval_asked_by_me;
  // Una solicitud abierta la retira o la cancela quien aprueba o quien la pidió (la de la IA no la
  // pidió nadie), con `quotes.update`.
  const closes =
    pending && (can("quotes.approve") || (quote.approval_requested_by_me && can("quotes.update")));
  const offers: [Verb, boolean][] = [
    ["request", status === "DRAFT" && can("quotes.update")],
    ["issue", (status === "DRAFT" || status === "APPROVED") && can("quotes.send")],
    ["accept", answerable && can("quotes.send")],
    ["decline", answerable && can("quotes.send")],
    ["approve", decides],
    ["reject", decides],
    ["withdraw", closes && can("quotes.update")],
    [
      "revise",
      ["APPROVED", "SENT", "VIEWED"].includes(status) &&
        can("quotes.create", "quotes.update", "quotes.cancel") &&
        (status === "APPROVED" || can("quotes.send")),
    ],
    ["cancel", can("quotes.cancel") && (OPEN.includes(status) || closes)],
  ];
  const shown = offers.flatMap(([verb, ok]) => (ok ? [verb] : []));
  const problem = open ? null : problemOf(run.error);
  // Un 409 que vuelve a leer la cotización puede quitar el botón: el error y el formulario siguen.
  if (shown.length === 0 && !open && !problem) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2" aria-busy={run.isPending}>
        {shown.map((verb) => (
          <Button
            key={verb}
            variant={FORWARD.includes(verb) ? "accent" : "ghost"}
            className={`min-h-11 sm:min-h-9 ${FORWARD.includes(verb) ? "" : "border-border border"}`}
            aria-disabled={run.isPending}
            aria-expanded={verb in TEXTS ? open === verb : undefined}
            onClick={() => {
              if (run.isPending) return;
              if (verb === "request" || verb === "withdraw") return go(verb);
              run.reset();
              onDone("");
              setOpen(verb);
            }}
          >
            {t(`verbs.${verb}`)}
          </Button>
        ))}
      </div>
      <p role="alert" className={problem ? "text-danger text-sm" : "sr-only"}>
        {problem}
      </p>
      {open ? (
        <Decision
          key={open}
          verb={open}
          quote={quote}
          busy={run.isPending}
          problem={problemOf(run.error)}
          onSend={(version, sent) => go(open, version, sent)}
          onCancel={() => run.isPending || setOpen(null)}
        />
      ) : null}
    </div>
  );
}

// Solo montado al revisar: el enrutador se pide cuando hace falta.
function GoTo({ href }: { href: string }) {
  const router = useRouter();
  useEffect(() => router.push(href), [router, href]);
  return null;
}

// El formulario de una acción: con la versión vista al abrir, sus textos y, al emitir, la validez
// (la que tiene si sigue vigente; si no, se elige otra, que solo viaja si cambió).
function Decision({
  verb,
  quote,
  busy,
  problem,
  onSend,
  onCancel,
}: {
  verb: Asked;
  quote: QuoteDetail;
  busy: boolean;
  problem: string | null;
  onSend: (version: number, sent: Sent) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("quotes.actions");
  const zone = useTimeZone() ?? ZONE;
  const id = useId();
  const form = useFocusTrip();
  const [{ version, today, opened }] = useState(() => {
    const today = localValue(new Date(), zone).slice(0, 10);
    const until = quote.valid_until ? localValue(new Date(quote.valid_until), zone) : "";
    const opened = until.slice(0, 10) >= today ? until.slice(0, 10) : "";
    return { version: quote.version ?? 1, today, opened };
  });
  const [day, setDay] = useState(opened);
  const [texts, setTexts] = useState<Sent>({});
  const [checked, setChecked] = useState(false);
  const fields = TEXTS[verb];
  const words = { number: quote.number, next: (quote.revision ?? 1) + 1 };
  const title = t(`titles.${verb}`, words);
  const missing = fields.some((x) => x.needed && !texts[x.name]?.trim());
  const dated = verb === "issue";
  const wrongDay = !dated
    ? null
    : !day
      ? "needDay"
      : day < today || day > DAYS.max
        ? "badDay"
        : null;
  const shown = checked && missing ? t("needReason") : checked && wrongDay ? t(wrongDay) : problem;

  return (
    <form
      ref={form}
      noValidate
      aria-label={title}
      aria-busy={busy}
      className="border-border bg-surface flex max-w-xl flex-col gap-3 rounded-lg border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        setChecked(true);
        if (missing || wrongDay) return;
        const sent: Sent = {};
        for (const { name } of fields) if (texts[name]?.trim()) sent[name] = texts[name].trim();
        if (dated && day !== opened)
          sent.valid_until = zonedInstant(`${day}T23:59`, zone).toISOString();
        onSend(version, sent);
      }}
    >
      <h2 className="font-semibold">{title}</h2>
      {t.has(`intros.${verb}`) ? (
        <p className="text-muted text-sm">{t(`intros.${verb}`, words)}</p>
      ) : null}
      {dated ? (
        <Field id={`${id}-until`} label={t("fields.valid_until")}>
          <input
            id={`${id}-until`}
            type="date"
            min={today}
            max={DAYS.max}
            value={day}
            readOnly={busy}
            aria-invalid={checked && wrongDay ? true : undefined}
            aria-describedby={`${id}-until-hint`}
            className={`${FIELD} h-11 sm:w-56`}
            onChange={(event) => setDay(event.target.value)}
          />
          <p id={`${id}-until-hint`} className="text-muted text-sm">
            {t("hints.valid_until", { zone })}
          </p>
        </Field>
      ) : null}
      {fields.map(({ name, max, needed }) => (
        <Field key={name} id={`${id}-${name}`} label={t(`fields.${dated ? name : verb}`)}>
          <textarea
            id={`${id}-${name}`}
            rows={3}
            value={texts[name] ?? ""}
            maxLength={max}
            readOnly={busy}
            aria-invalid={checked && needed && missing ? true : undefined}
            aria-describedby={`${id}-${name}-hint`}
            className={`${FIELD} min-h-20 resize-y py-2.5`}
            onChange={(event) => setTexts((all) => ({ ...all, [name]: event.target.value }))}
          />
          <p id={`${id}-${name}-hint`} className="text-muted text-sm">
            {t(`hints.${dated ? name : verb}`, { max })}
          </p>
        </Field>
      ))}
      <p role="alert" className={shown ? "text-danger text-sm" : "sr-only"}>
        {shown}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="submit"
          variant={FORWARD.includes(verb) ? "accent" : "primary"}
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
        >
          {t(`confirm.${verb}`, words)}
        </Button>
        <Button
          type="button"
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={onCancel}
        >
          {t("back")}
        </Button>
      </div>
    </form>
  );
}
