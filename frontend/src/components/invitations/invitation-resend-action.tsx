"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type invitationsList, useInvitationsResend } from "@/lib/api/client";
import type { Invitation } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

type Pages = InfiniteData<Awaited<ReturnType<typeof invitationsList>>>;
// Por qué no se puede reenviar ahora, y hasta cuándo (milisegundos de época).
type Block = { reason: "maxed" | "soon" | "daily" | "just"; until?: number };

export const MAX_SENDS = 5; // los envíos de una invitación (ADR-020 §3)
const GAP = 15 * 60_000; // entre dos envíos al mismo correo
const WAITS: Record<string, Block["reason"]> = {
  INVITATION_SEND_TOO_SOON: "soon",
  INVITATION_ADDRESS_LIMIT: "daily",
};

// Reenviar una invitación pendiente, caducada o no (F2-130f), con `POST …/invitations/{id}/resend/`
// (F2-130e): la renueva con sus roles, el enlace anterior deja de valer y sale un correo nuevo.
// Lo que la pantalla sabe de antemano (5 envíos, 15 minutos desde el último) lo dice antes de
// pulsar; el tope diario por correo solo lo sabe la API, y su `Retry-After` dice hasta cuándo.
export function InvitationResendAction({
  slug,
  invitation,
  listKey,
  onAsk,
  onSettle,
}: {
  slug: string;
  invitation: Invitation;
  listKey: QueryKey;
  onAsk: () => void; // se pulsa: el aviso anterior de la lista ya no aplica
  onSettle: (text: string, stale: boolean, here: boolean) => void; // como «Revocar»
}) {
  const t = useTranslations("invitations.resend");
  const errors = useTranslations("errors.api");
  const format = useFormatter();
  const queryClient = useQueryClient();
  const why = useId();
  const root = useRef<HTMLDivElement>(null);
  const sending = useRef(false); // dos pulsaciones seguidas no son dos peticiones
  const [now, setNow] = useState(() => Date.now());
  const [held, setHeld] = useState<Block | null>(null); // lo que dijo la API al pulsar
  // Cuándo se vio este `sent_at` por primera vez: un momento fijo, no `now`, que avanza.
  const [seen, setSeen] = useState(() => ({ sent: invitation.sent_at, at: Date.now() }));
  const fresh = seen.sent === invitation.sent_at;
  useEffect(() => {
    if (fresh) return;
    const timer = setTimeout(() => setSeen({ sent: invitation.sent_at, at: Date.now() }));
    return () => clearTimeout(timer);
  }, [fresh, invitation.sent_at]);
  const { email } = invitation;
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  const resend = useInvitationsResend({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved, { invitationId }) => {
        // Como «Revocar»: una lectura en vuelo traería la fila de antes y se cancela.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) => (row.id === invitationId ? saved : row)),
                })),
              }
            : data,
        );
        // La tarea cuenta el envío después: hasta entonces, la fila aún dice el anterior.
        setHeld({ reason: "just", until: Date.now() + GAP });
        onSettle(t("done", { email }), false, false);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        const gone = error.status === 404;
        if (gone || error.code === "INVALID_TRANSITION") {
          return onSettle(t(gone ? "stale" : "settled", { email }), true, here());
        }
        if (error.code === "INVITATION_SEND_LIMIT") setHeld({ reason: "maxed" });
        const wait = WAITS[error.code];
        if (wait && error.retryAfter) {
          setHeld({ reason: wait, until: Date.now() + error.retryAfter * 1000 });
        }
      },
    },
  });
  const busy = resend.isPending || (resend.isError && resend.error.status === 401);
  useLayoutEffect(() => {
    if (!busy) sending.current = false; // como «Revocar»: cuando la pantalla ya lo enseña
  });

  // Con el reloj atrasado, como mucho 15 minutos desde que se vio ese envío (hasta anotarlo, un
  // instante, sin ese tope); quien manda es el 429 de la API con su `Retry-After`.
  const seenUntil = fresh ? seen.at + GAP : Infinity;
  const free = Math.min(Date.parse(invitation.sent_at ?? "") + GAP, seenUntil);
  const block: Block | null =
    held?.reason === "maxed"
      ? held
      : invitation.send_count >= MAX_SENDS
        ? { reason: "maxed" }
        : held?.until && held.until > now
          ? held
          : free > now
            ? { reason: "soon", until: free }
            : null;
  const until = block?.until;
  useEffect(() => {
    // `now` solo avanza aquí: al llegar la hora, se vuelve a ofrecer.
    if (until === undefined) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.max(0, until - Date.now()) + 50);
    return () => clearTimeout(timer);
  }, [until]);

  if (invitation.status !== "PENDING" && invitation.status !== "EXPIRED") return null;
  // Hasta cuándo: la hora; si es el tope diario, también el día.
  const style = block?.reason === "daily" ? ({ dateStyle: "medium" } as const) : {};
  const shown = (moment = 0) => format.dateTime(moment, { ...style, timeStyle: "short" });
  const reason = block ? t(block.reason, { when: shown(block.until) }) : null;
  const failure = resend.isError && !held && !busy ? explain(resend.error) : null;
  function explain(error: ApiError) {
    if (error.status === 404 || error.code === "INVALID_TRANSITION") return null; // la lista
    if (error.code === "PERMISSION_DENIED") return t("denied");
    if (error.code === "INVITATION_ROLE_GONE") return t("roleGone");
    if (error.code === "ALREADY_MEMBER") return t("member");
    if (WAITS[error.code]) return t(WAITS[error.code] === "soon" ? "soonLater" : "dailyLater");
    // Sin campos que revisar: un 400 es un fallo nuestro.
    return errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }
  // Lo que respondió la API al pulsar se anuncia; lo que la fila ya decía, no.
  const said = !!block && block === held && block.reason !== "just";

  return (
    <div
      ref={root}
      className="flex flex-col items-start gap-2"
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {/* `aria-disabled` y no `disabled`: conserva el foco y el motivo se sigue leyendo. */}
      <Button
        variant="ghost"
        className="border-border min-h-11 border aria-disabled:cursor-not-allowed aria-disabled:opacity-60 sm:min-h-9"
        aria-label={t("label", { email })}
        aria-disabled={busy || !!block}
        aria-describedby={reason || failure ? why : undefined}
        onClick={() => {
          if (sending.current || busy || block) return;
          sending.current = true;
          setHeld(null);
          onAsk();
          resend.mutate({ orgSlug: slug, invitationId: invitation.id });
        }}
      >
        {t(busy ? "busy" : "open")}
      </Button>
      {reason || failure ? (
        <p
          id={why}
          role={said || failure ? "alert" : undefined}
          className={failure ? "text-danger text-sm" : "text-muted text-sm"}
        >
          {reason ?? failure}
        </p>
      ) : null}
    </div>
  );
}
