"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Hand } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationHumanRequestClose,
  conversationHumanRequestCreate,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type { Conversation, ConversationDetail, HumanRequestState } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { useMembers } from "./members";
import { Plain } from "./plain";

const WHEN = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } as const;
const REASONS = ["ASKED_FOR_PERSON", "UPSET", "MODEL", "AGENT"] as const;

// La solicitud abierta, en una fila de la conversación: el motivo (un código cerrado; otro, el de
// quien atiende), quién la pidió (si se conoce su nombre) y cuándo.
function HumanRequestLine({ conversation }: { conversation: Conversation }) {
  const t = useTranslations("inbox.humanRequest");
  const format = useFormatter();
  const { user } = useTenant();
  const { name } = useMembers();
  const open = conversation.human_request;
  if (!open) return null;
  const reason = REASONS.find((one) => one === open.reason) ?? "AGENT";
  const who = open.requested_by_user_id === user.id ? t("you") : name(open.requested_by_user_id);
  return (
    <p className="bg-accent/40 border-foreground/30 flex items-start gap-1.5 rounded-[10px] border px-2 py-1 text-xs lg:text-sm">
      <Hand aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>
        <strong className="font-semibold">{t("open")}</strong> {t(`reason.${reason}`)}{" "}
        {who === null ? null : (
          <>
            {t("by")} <Plain>{who}</Plain> ·{" "}
          </>
        )}
        <time dateTime={open.requested_at}>
          {format.dateTime(new Date(open.requested_at), WHEN)}
        </time>
      </span>
    </p>
  );
}

// Solicitar atención humana (F8-08n, F8-09b): `POST …/human-request/` la pide y `DELETE` la
// cierra, con `conversations.view` y su alcance, como ver la conversación. Las dos son idempotentes
// y ninguna envía nada al cliente ni asigna la conversación. Da `button`, en la fila del
// encabezado (solo el icono por debajo de 1024 px), y `state`: la solicitud abierta y lo que pasó.
// En una conversación cerrada no se pide (la API responde 409), pero una abierta se puede cerrar.
export function useHumanRequest(conversation: Conversation) {
  const t = useTranslations("inbox.humanRequest");
  const errors = useTranslations("errors");
  const { organization } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const busy = useRef(false); // un clic doble antes de pintar `isPending` no pide dos veces
  // Lo que pasó, con el estado al que se refiere: si la solicitud cambia por otra vía (el copiloto,
  // otra persona), el aviso ya no vale y no se muestra.
  const [said, setSaid] = useState<{ error: boolean; text: string; at: string | null } | null>(
    null,
  );
  const at = conversation.human_request?.requested_at ?? null;
  const detailKey = getConversationsRetrieveQueryKey(slug, conversation.id);
  const change = useMutation<HumanRequestState, ApiError, boolean>({
    mutationFn: (asking) =>
      asking
        ? conversationHumanRequestCreate(slug, conversation.id)
        : conversationHumanRequestClose(slug, conversation.id),
    onMutate: () => setSaid(null),
    onSuccess: ({ human_request }, asking) => {
      // El estado nuevo se ve al instante; la relectura lo confirma.
      client.setQueryData<ConversationDetail>(detailKey, (old) => old && { ...old, human_request });
      const text = t(asking ? "requested" : "closed");
      setSaid({ error: false, text, at: human_request?.requested_at ?? null });
    },
    onError: (error) =>
      setSaid({
        error: true,
        at,
        text:
          error.status === 403 || error.status === 404
            ? t("denied")
            : errors(`api.${apiErrorKey(error)}`),
      }),
    onSettled: () => {
      busy.current = false;
      void client.invalidateQueries({ queryKey: detailKey });
      void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
    },
  });
  const asking = !conversation.human_request;
  const label = t(change.isPending ? "saving" : asking ? "request" : "close");

  const button =
    asking && conversation.status === "CLOSED" ? null : (
      <Button
        variant={asking ? "ghost" : "accent"}
        className="border-border order-3 border max-lg:size-11 max-lg:px-0 lg:order-2"
        title={label}
        aria-disabled={change.isPending}
        onClick={() => {
          if (busy.current || change.isPending) return;
          busy.current = true;
          change.mutate(asking);
        }}
      >
        <Hand aria-hidden />
        <span className="max-lg:sr-only">{label}</span>
      </Button>
    );
  const state = (
    <div className="order-5 flex basis-full flex-col gap-1 empty:hidden">
      <HumanRequestLine conversation={conversation} />
      {said && said.at === at ? (
        <p
          role={said.error ? "alert" : "status"}
          className={said.error ? "text-danger text-sm" : "text-sm"}
        >
          {said.text}
        </p>
      ) : null}
    </div>
  );
  return { button, state };
}
