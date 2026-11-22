"use client";

import { useMutation } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationsCopilotSuggest,
  conversationsCopilotSuggestionInsert,
} from "@/lib/api/client";
import type { CopilotFinding, CopilotInsert, CopilotSuggestion } from "@/lib/api/model";
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

function Marked({ text, findings, label }: CopilotSuggestion & { label: string }) {
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

// Qué decir ante un error, por código y nunca con el texto del servidor; con `Retry-After`, cuánto.
function problem(error: Error, t: ReturnType<typeof useTranslations>) {
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
// llega al cuadro de respuesta, editable, y sale solo con su «Enviar».
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
  const suggestButton = useRef<HTMLButtonElement>(null);
  const [draft, setDraft] = useState<CopilotSuggestion | null>(null);
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
      setStatus(t("ready"));
    },
    onError: (failure) => {
      setStatus("");
      setError(problem(failure, t));
    },
    onSettled: () => (asking.current = false),
  });
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

  const trigger = (
    <Button
      ref={suggestButton}
      type="button"
      variant="accent"
      className="h-9 shrink-0 aria-disabled:cursor-progress aria-disabled:opacity-60 max-sm:w-10 max-sm:px-0"
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
      {draft || error ? (
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
          {draft ? (
            <div className="bg-surface border-border flex flex-col gap-2 rounded-[10px] border p-3">
              <p className="text-muted text-xs font-semibold uppercase">{t("draft")}</p>
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
                  className="h-9 aria-disabled:cursor-progress aria-disabled:opacity-60"
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
                  className="border-border h-9 border"
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
  return { trigger, panel };
}
