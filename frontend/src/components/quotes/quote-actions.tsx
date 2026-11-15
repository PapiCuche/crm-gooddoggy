"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  quotesApprove,
  quotesReject,
  quotesRequestApproval,
  quotesWithdrawRequest,
} from "@/lib/api/client";
import type { QuoteDetail } from "@/lib/api/model";

import {
  Field,
  FIELD,
  focusHome,
  useFocusTrip,
  useQuoteProblem,
  useQuoteWrite,
} from "./quote-editor";
import { useHeld } from "./quote-parts";

type Verb = "request" | "withdraw" | "approve" | "reject";
// Lo que se escribe al decidir: el comentario de aprobar es opcional; el motivo de rechazar, no.
const TEXT = { approve: { max: 500, needed: false }, reject: { max: 200, needed: true } } as const;

// Las acciones de la aprobación de una cotización (F5-09h), cada una con su estado y su permiso (la
// API los vuelve a comprobar): enviar un borrador a aprobación, retirar la solicitud (quien la pidió
// o quien aprueba), y aprobar o rechazarla (quien aprueba, nunca quien la pidió: lo dice la API en
// `approval_asked_by_me`). Se manda la `version` que se ve; un 409 vuelve a leer el detalle y se
// explica por su código. Aprobar lleva un comentario opcional y rechazar, su motivo.
export function QuoteActions({
  quote,
  onDone,
}: {
  quote: QuoteDetail;
  onDone: (said: string) => void;
}) {
  const t = useTranslations("quotes.actions");
  const slug = useTenant().organization.slug;
  const held = useHeld();
  const problemOf = useQuoteProblem();
  const [open, setOpen] = useState<"approve" | "reject" | null>(null);
  const job = useRef<{ verb: Verb; version: number; text: string }>(null);
  const moved = useRef(false);
  useEffect(() => {
    // Tras una acción, su botón puede ya no ofrecerse: el foco no se pierde.
    if (moved.current) focusHome();
    moved.current = false;
  });
  const run = useQuoteWrite(
    quote.id,
    () => {
      const { verb, version, text } = job.current!;
      if (verb === "request") return quotesRequestApproval(slug, quote.id, { version });
      if (verb === "withdraw") return quotesWithdrawRequest(slug, quote.id, { version });
      if (verb === "approve")
        return quotesApprove(slug, quote.id, { version, ...(text ? { comment: text } : {}) });
      return quotesReject(slug, quote.id, { version, reason: text });
    },
    (saved) => {
      const verb = job.current!.verb;
      moved.current = true;
      setOpen(null);
      onDone(
        t(verb === "request" && saved.status === "APPROVED" ? "done.approvedNow" : `done.${verb}`),
      );
    },
  );
  const go = (verb: Verb, version = quote.version ?? 1, text = "") => {
    if (run.isPending) return;
    job.current = { verb, version, text };
    onDone("");
    run.mutate();
  };
  const pending = quote.status === "PENDING_APPROVAL";
  const decides = pending && held("quotes.approve", quote) && !quote.approval_asked_by_me;
  const shown: Verb[] = [
    ...(quote.status === "DRAFT" && held("quotes.update", quote) ? ["request" as const] : []),
    ...(decides ? (["approve", "reject"] as const) : []),
    ...(pending &&
    held("quotes.update", quote) &&
    (quote.approval_asked_by_me || held("quotes.approve", quote))
      ? ["withdraw" as const]
      : []),
  ];
  const problem = open ? null : problemOf(run.error);
  // Un 409 que vuelve a leer la cotización puede quitar el botón: el error y el formulario siguen.
  if (shown.length === 0 && !open && !problem) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2" aria-busy={run.isPending}>
        {shown.map((verb) => (
          <Button
            key={verb}
            variant={verb === "approve" || verb === "request" ? "accent" : "ghost"}
            className={`min-h-11 sm:min-h-9 ${verb === "approve" || verb === "request" ? "" : "border-border border"}`}
            aria-disabled={run.isPending}
            aria-expanded={verb in TEXT ? open === verb : undefined}
            onClick={() => {
              if (verb === "approve" || verb === "reject") {
                run.reset();
                onDone("");
                setOpen(verb);
              } else go(verb);
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
          onSend={(version, text) => go(open, version, text)}
          onCancel={() => run.isPending || setOpen(null)}
        />
      ) : null}
    </div>
  );
}

// Aprobar o rechazar una solicitud: con la versión vista al abrir, y su texto.
function Decision({
  verb,
  quote,
  busy,
  problem,
  onSend,
  onCancel,
}: {
  verb: "approve" | "reject";
  quote: QuoteDetail;
  busy: boolean;
  problem: string | null;
  onSend: (version: number, text: string) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("quotes.actions");
  const id = useId();
  const form = useFocusTrip();
  const [version] = useState(quote.version ?? 1);
  const [text, setText] = useState("");
  const [checked, setChecked] = useState(false);
  const { max, needed } = TEXT[verb];
  const missing = needed && text.trim() === "";
  const shown = checked && missing ? t("needReason") : problem;

  return (
    <form
      ref={form}
      noValidate
      aria-label={t(`titles.${verb}`, { number: quote.number })}
      aria-busy={busy}
      className="border-border bg-surface flex max-w-xl flex-col gap-3 rounded-lg border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        setChecked(true);
        if (!missing) onSend(version, text.trim());
      }}
    >
      <h2 className="font-semibold">{t(`titles.${verb}`, { number: quote.number })}</h2>
      <Field id={`${id}-text`} label={t(`fields.${verb}`)}>
        <textarea
          id={`${id}-text`}
          rows={3}
          value={text}
          maxLength={max}
          readOnly={busy}
          aria-invalid={checked && missing ? true : undefined}
          aria-describedby={`${id}-hint`}
          className={`${FIELD} min-h-20 resize-y py-2.5`}
          onChange={(event) => setText(event.target.value)}
        />
        <p id={`${id}-hint`} className="text-muted text-sm">
          {t(`hints.${verb}`, { max })}
        </p>
      </Field>
      <p role="alert" className={shown ? "text-danger text-sm" : "sr-only"}>
        {shown}
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="submit"
          variant={verb === "approve" ? "accent" : "primary"}
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
        >
          {t(`confirm.${verb}`)}
        </Button>
        <Button
          type="button"
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={onCancel}
        >
          {t("cancel")}
        </Button>
      </div>
    </form>
  );
}
