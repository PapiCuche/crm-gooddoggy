"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Hand, ScrollText, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationsCopilotSuggest,
  conversationsCopilotSuggestionInsert,
  conversationsCopilotSummary,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type {
  CopilotFinding,
  CopilotHandoff,
  CopilotInsert,
  CopilotSuggestion,
  CopilotSummary,
} from "@/lib/api/model";
import { ApiError } from "@/lib/http";

import { Plain } from "./plain";

export const AI_USE = "ai.use";

// Las posiciones de la API cuentan puntos de código (como Python); `String` cuenta unidades UTF-16:
// un emoji antes de la marca la correría un lugar. Se convierten, se descartan las que no caben o se
// solapan y se ordenan. Nada del texto se interpreta: solo se parte en tramos.
export function utf16Ranges(text: string, findings: readonly CopilotFinding[]) {
  const offsets = [0]; // `offsets[i]`: dónde empieza en UTF-16 el punto de código `i`
  let at = 0;
  for (const char of text) offsets.push((at += char.length));
  const ranges: [number, number][] = [];
  for (const { start, end } of findings.map(({ position }) => position)) {
    const from = offsets[start]; // fuera del texto, negativa o no entera: `undefined`
    const to = offsets[end];
    if (from !== undefined && to !== undefined && to > from) ranges.push([from, to]);
  }
  const kept: [number, number][] = [];
  for (const range of ranges.sort((a, b) => a[0] - b[0])) {
    if (range[0] >= (kept.at(-1)?.[1] ?? 0)) kept.push(range);
  }
  return kept;
}

type Shown = { text: string; findings: readonly CopilotFinding[]; label: string };
function Marked({ text, findings, label }: Shown) {
  const parts: ReactNode[] = [];
  let at = 0;
  for (const [start, end] of utf16Ranges(text, findings)) {
    parts.push(text.slice(at, start));
    parts.push(
      <mark
        key={start}
        title={label}
        className="bg-danger/10 text-danger rounded-[4px] px-0.5 font-semibold underline decoration-dotted underline-offset-2"
      >
        {text.slice(start, end)}
      </mark>,
    );
    at = end;
  }
  parts.push(text.slice(at));
  return <Plain>{parts}</Plain>;
}

const REASONS = ["ASKED_FOR_PERSON", "UPSET", "MODEL"] as const;
// El motivo del traspaso es un código: solo uno conocido elige su texto; otro, el genérico.
const why = (handoff: CopilotHandoff) => REASONS.find((one) => one === handoff.reason) ?? "other";

// El aviso de traspaso (ADR-034 §8): el cliente pide una persona. El servidor ya registró la
// solicitud de atención humana; aquí solo se avisa, nada sale al cliente.
function Handoff({ handoff, t }: { handoff: CopilotHandoff; t: Translate }) {
  if (!handoff.requested) return null;
  return (
    <div className="bg-accent/40 border-foreground/30 flex gap-2 rounded-[10px] border p-2 text-sm">
      <Hand aria-hidden className="mt-0.5 size-4 shrink-0" />
      <p>
        <strong className="font-semibold">{t(`handoff.${why(handoff)}`)}</strong>{" "}
        {t("handoff.next")}
      </p>
    </div>
  );
}

type Translate = ReturnType<typeof useTranslations>;
// Qué decir ante un error, por código y nunca con el texto del servidor; con `Retry-After`, cuánto.
function problem(error: Error, t: Translate) {
  if (!(error instanceof ApiError)) return t("error.failed");
  const { code, status, retryAfter } = error;
  if (code === "RATE_LIMITED" || code === "AI_ORG_RATE_LIMITED") {
    return retryAfter
      ? t(`error.${code}_WAIT`, { minutes: Math.max(1, Math.ceil(retryAfter / 60)) })
      : t(`error.${code}`);
  }
  const known = [
    "AI_BUDGET_EXHAUSTED",
    "AI_BUDGET_NOT_SET",
    "AI_DISABLED",
    "AI_NOT_CONFIGURED",
    "AI_DEADLINE",
    "AI_EVIDENCE_EXPIRED",
    "AI_EXCLUDED",
  ] as const;
  const found = known.find((name) => name === code);
  if (found) return t(`error.${found}`);
  if (status === 403 || status === 404) return t("error.denied"); // como el cuadro de respuesta
  return t("error.failed");
}

// El copiloto en la conversación (F8-09a, ADR-034 §9), con `ai.use`. Como las respuestas rápidas, da
// dos piezas: `trigger`, «Sugerir respuesta» (`POST …/copilot/suggest/`), que va en la fila de los
// modos del cuadro y no ocupa otra fila; y `panel`, sobre el cuadro solo con una sugerencia o un
// error: la sugerencia con lo que no tiene respaldo marcado, «Insertar» (`POST
// …/suggestions/{id}/insert/`, que la audita) y «Descartar». Ningún camino de aquí envía: el texto
// llega al cuadro de respuesta, editable, y sale solo con su «Enviar». F8-09b suma «Resumir» (`POST
// …/copilot/summary/`): el resumen es solo para leer, sin «Insertar» ni nada que lo envíe; y el aviso
// de traspaso cuando una respuesta lo marca.
export function useCopilot({
  conversationId,
  onInsert,
}: {
  conversationId: string;
  onInsert: (text: string) => void; // pone el texto en el cuadro de respuesta y lo enfoca
}) {
  const t = useTranslations("inbox.copilot");
  const { organization } = useTenant();
  const slug = organization.slug;
  const heading = useId();
  const asking = useRef(false); // un clic doble antes de pintar `isPending` no pide dos veces
  const inserting = useRef(false); // ni inserta (y audita) dos veces
  const summing = useRef(false);
  const client = useQueryClient();
  const suggestButton = useRef<HTMLButtonElement>(null);
  const summaryButton = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState<CopilotSuggestion | null>(null);
  const [summary, setSummary] = useState<CopilotSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");

  const suggest = useMutation<CopilotSuggestion, ApiError>({
    mutationFn: () => conversationsCopilotSuggest(slug, conversationId),
    onMutate: () => {
      setError(null);
      setStatus(t("thinking"));
    },
    onSuccess: (done) => {
      setDraft(done);
      announce(t("ready"), done.handoff);
    },
    onError: (failure) => {
      setStatus("");
      setError(problem(failure, t));
    },
    onSettled: () => (asking.current = false),
  });
  const summarize = useMutation<CopilotSummary, ApiError>({
    mutationFn: () => conversationsCopilotSummary(slug, conversationId),
    onMutate: () => {
      setError(null);
      setStatus(t("summarizing"));
    },
    onSuccess: (done) => {
      setSummary(done);
      announce(t("summarized"), done.handoff);
    },
    onError: (failure) => {
      setStatus("");
      setError(problem(failure, t));
    },
    onSettled: () => (summing.current = false),
  });
  // Con traspaso el servidor registró la solicitud: la conversación y la lista se vuelven a leer.
  function announce(text: string, handoff: CopilotHandoff) {
    setStatus(handoff.requested ? `${text} ${t(`handoff.${why(handoff)}`)}` : text);
    if (!handoff.requested) return;
    void client.invalidateQueries({
      queryKey: getConversationsRetrieveQueryKey(slug, conversationId),
    });
    void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
  }
  const insert = useMutation<CopilotInsert, ApiError, CopilotSuggestion>({
    mutationFn: ({ id }) => conversationsCopilotSuggestionInsert(slug, conversationId, id),
    onMutate: () => setError(null),
    onSuccess: (done) => {
      setDraft(null);
      setStatus(t("inserted"));
      onInsert(done.text); // el texto guardado, el mismo que se auditó
    },
    onError: (failure) => {
      // Ya no se puede insertar (purgada, de otra persona o con la evidencia caducada): se quita y
      // el foco, que estaba en «Insertar», pasa a «Sugerir respuesta», como al descartar.
      if (failure.status === 404 || failure.code === "AI_EVIDENCE_EXPIRED") {
        setDraft(null);
        suggestButton.current?.focus();
      }
      setError(failure.status === 404 ? t("error.gone") : problem(failure, t));
    },
    onSettled: () => (inserting.current = false),
  });
  const busy = suggest.isPending || insert.isPending;
  const label = suggest.isPending ? t("thinking") : draft ? t("again") : t("suggest");
  const sumLabel = t(summarize.isPending ? "summarizing" : summary ? "resummarize" : "summarize");

  // «Resumir», junto a «Sugerir respuesta» en la fila de los modos; también solo el icono en 390 px.
  const summarizer = (
    <Button
      ref={summaryButton}
      type="button"
      variant="ghost"
      className="border-border h-9 shrink-0 border aria-disabled:cursor-progress aria-disabled:opacity-60 max-lg:min-h-11 max-sm:w-11 max-sm:px-0"
      aria-disabled={summarize.isPending}
      onClick={() => {
        if (summing.current || summarize.isPending) return;
        summing.current = true;
        summarize.mutate();
      }}
    >
      <ScrollText aria-hidden />
      <span className="max-sm:sr-only">{sumLabel}</span>
    </Button>
  );
  const trigger = (
    <Button
      ref={suggestButton}
      type="button"
      variant="accent"
      className="h-9 shrink-0 aria-disabled:cursor-progress aria-disabled:opacity-60 max-lg:min-h-11 max-sm:w-11 max-sm:px-0"
      aria-disabled={busy}
      onClick={() => {
        if (asking.current || busy) return;
        asking.current = true;
        suggest.mutate();
      }}
    >
      <Sparkles aria-hidden />
      {/* En 390 px solo el icono: el nombre accesible es el mismo. */}
      <span className="max-sm:sr-only">{label}</span>
    </Button>
  );

  const panel = (
    <>
      <p role="status" aria-live="polite" className="sr-only">
        {status}
      </p>
      {draft || summary || error ? (
        <section
          aria-labelledby={heading}
          className="border-border bg-surface-raised flex max-h-[45%] min-h-0 shrink-0 flex-col gap-2 overflow-y-auto border-t p-2 lg:p-3"
        >
          <h3 id={heading} className="flex items-center gap-1.5 text-sm font-semibold">
            <Sparkles aria-hidden className="size-4" />
            {t("label")}
          </h3>
          {error ? (
            <p role="alert" className="text-danger text-sm">
              {error}
            </p>
          ) : null}
          {summary ? (
            <div className="bg-surface border-border flex flex-col gap-2 rounded-[10px] border p-3">
              <p className="text-muted flex flex-wrap items-center gap-x-2 text-xs font-semibold uppercase">
                <ScrollText aria-hidden className="size-3.5" />
                {t("summary")}
                <span className="bg-surface-raised border-border rounded-full border px-2 normal-case">
                  {t("generated")}
                </span>
              </p>
              <Handoff handoff={summary.handoff} t={t} />
              <p className="text-sm leading-snug">
                {/* Los tramos del resumen llegan con su posición suelta; se marcan igual. */}
                <Marked
                  text={summary.text}
                  findings={summary.marks.map(({ type, ...position }) => ({ type, position }))}
                  label={t("mark")}
                />
              </p>
              {summary.marks.length > 0 ? (
                <p className="text-muted text-xs">{t("summaryUnverified")}</p>
              ) : null}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  className="border-border h-9 border max-lg:min-h-11"
                  onClick={() => {
                    setSummary(null);
                    setStatus(t("summaryClosed"));
                    summaryButton.current?.focus();
                  }}
                >
                  {t("closeSummary")}
                </Button>
                <p className="text-muted text-xs">{t("readOnly")}</p>
              </div>
            </div>
          ) : null}
          {draft ? (
            <div className="bg-surface border-border flex flex-col gap-2 rounded-[10px] border p-3">
              <p className="text-muted text-xs font-semibold uppercase">{t("draft")}</p>
              <Handoff handoff={draft.handoff} t={t} />
              <p className="text-sm leading-snug">
                <Marked {...draft} label={t("mark")} />
              </p>
              {draft.findings.length > 0 ? (
                <p className="text-muted text-xs">{t("unverified")}</p>
              ) : null}
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="primary"
                  className="h-9 aria-disabled:cursor-progress aria-disabled:opacity-60 max-lg:min-h-11"
                  aria-disabled={busy}
                  onClick={() => {
                    if (inserting.current || busy) return;
                    inserting.current = true;
                    insert.mutate(draft);
                  }}
                >
                  {insert.isPending ? t("inserting") : t("insert")}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  className="border-border h-9 border max-lg:min-h-11"
                  disabled={insert.isPending}
                  onClick={() => {
                    setDraft(null);
                    setError(null);
                    setStatus(t("discarded"));
                    suggestButton.current?.focus();
                  }}
                >
                  {t("discard")}
                </Button>
              </div>
              <p className="text-muted text-xs">{t("never")}</p>
            </div>
          ) : null}
        </section>
      ) : null}
    </>
  );
  return { trigger, summarizer, panel };
}
