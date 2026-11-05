"use client";

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationMessagesSend,
  getConversationMessagesListQueryKey,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type {
  Conversation,
  Grant,
  Message,
  PaginatedMessageList,
  SendMessageRequest,
} from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

// El tope de la API (`BODY_MAX`, 4096 caracteres; se cuentan puntos de código, como ella).
export const BODY_MAX = 4096;
type Pages = InfiniteData<PaginatedMessageList, string>;

export const messagesKey = (slug: string, id: string) =>
  [...getConversationMessagesListQueryKey(slug, id), "pages"] as const;

// ¿Se ofrece el cuadro? Es comodidad, no seguridad: la API decide en cada envío. Con alcance solo
// `OWN` basta con que la conversación sea de quien escribe; con equipo o sucursal no se sabe desde
// aquí y se ofrece (si la API responde 404, se explica).
export function canSend(grants: readonly Grant[], conversation: Conversation, userId: string) {
  const grant = grants.find(({ code }) => code === "messages.send");
  if (!grant) return false;
  const { scopes } = grant;
  if (scopes.length === 0 || scopes.some((scope) => scope !== "OWN")) return true;
  return conversation.assigned_user_id === userId;
}

const uuid = () => crypto.randomUUID();

// Cambia los mensajes de cada página de la caché (la primera página es la más reciente).
const edit = (data: Pages | undefined, change: (rows: Message[], newest: boolean) => Message[]) =>
  data && {
    ...data,
    pages: data.pages.map((page, i) => ({ ...page, results: change(page.results, i === 0) })),
  };

// El cuadro de redacción (F6-04c): `POST …/conversations/{id}/messages/` con un `client_message_id`
// del navegador. El mismo texto reintentado lleva el mismo id (la API lo toma por el mismo mensaje y
// no lo duplica); otro texto, o un envío ya hecho, uno nuevo. El mensaje aparece al instante «En
// cola» (optimista); si falla, se quita, el texto sigue en el cuadro y «Reintentar» lo reenvía.
export function Composer({
  conversation,
  onQueued,
}: {
  conversation: Conversation;
  onQueued: () => void;
}) {
  const t = useTranslations("inbox.composer");
  const errors = useTranslations("errors");
  const { organization, user } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const field = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const attempt = useRef<{ id: string; body: string } | null>(null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const key = messagesKey(slug, conversation.id);
  const count = Array.from(text).length;
  const closed = conversation.status === "CLOSED";
  const over = count > BODY_MAX;
  const empty = text.trim() === "";

  const send = useMutation<Message, ApiError, SendMessageRequest>({
    mutationFn: (request) => conversationMessagesSend(slug, conversation.id, request),
    onMutate: async (request) => {
      await client.cancelQueries({ queryKey: key });
      onQueued();
      const pending: Message = {
        ...{ id: `pending-${request.client_message_id}`, direction: "OUTBOUND" },
        ...{ sender_type: "USER", sender_user_id: user.id, message_type: "TEXT" },
        ...{ body: request.body, client_message_id: request.client_message_id },
        ...{ delivery_status: "QUEUED", created_at: new Date().toISOString() },
        ...{ sent_at: null, delivered_at: null, read_at: null, failed_at: null },
      };
      client.setQueryData<Pages>(key, (data) =>
        edit(data, (rows, newest) => (newest ? [pending, ...rows] : rows)),
      );
      setProblem(null);
    },
    onSuccess: (saved, request) => {
      attempt.current = null; // un id por mensaje: el siguiente texto lleva otro
      setText("");
      const mine = `pending-${request.client_message_id}`;
      client.setQueryData<Pages>(key, (data) => {
        const known = data?.pages.some((page) => page.results.some(({ id }) => id === saved.id));
        return edit(data, (rows, newest) => [
          ...(newest && !known ? [saved] : []),
          ...rows.filter(({ id }) => id !== mine),
        ]);
      });
      // El estado de entrega avanza en el servidor: se vuelve a pedir; la lista cambia su vista previa.
      void client.invalidateQueries({ queryKey: key });
      void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
      box.current?.focus();
    },
    onError: (error, request) => {
      client.setQueryData<Pages>(key, (data) =>
        edit(data, (rows) =>
          rows.filter(({ id }) => id !== `pending-${request.client_message_id}`),
        ),
      );
      // Ese id ya es de otro envío: reintentar con él chocaría siempre.
      if (error.code === "CLIENT_MESSAGE_ID_TAKEN") attempt.current = null;
      if (error.code === "CONVERSATION_CLOSED" || error.code === "CHANNEL_UNAVAILABLE") {
        void client.invalidateQueries({
          queryKey: getConversationsRetrieveQueryKey(slug, conversation.id),
        });
      }
      setProblem(
        error.status === 403 || error.status === 404
          ? t("denied")
          : errors(`api.${apiErrorKey(error)}`),
      );
    },
  });

  const submit = () => {
    if (send.isPending || closed || over || empty) return;
    // El mismo texto reintentado reutiliza su id; un texto distinto empieza un intento nuevo.
    if (attempt.current?.body !== text) attempt.current = { id: uuid(), body: text };
    send.mutate({ client_message_id: attempt.current.id, body: text });
  };
  const keys = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter envía; Mayús+Enter agrega una línea. Durante una composición (IME) Enter es del IME.
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    submit();
  };

  return (
    <form
      className="border-border flex min-w-0 flex-col gap-2 border-t p-3"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label htmlFor={field} className="text-sm font-medium">
        {t("label")}
      </label>
      {closed ? (
        <p id={`${field}-closed`} className="text-muted text-sm">
          {errors("api.CONVERSATION_CLOSED")}
        </p>
      ) : null}
      {/* 16 px: por debajo, Safari en iOS amplía la página al enfocar. */}
      <textarea
        id={field}
        ref={box}
        rows={2}
        value={text}
        disabled={closed}
        readOnly={send.isPending}
        aria-describedby={`${field}-count ${field}-hint`}
        aria-invalid={over || problem ? true : undefined}
        className="bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger max-h-40 min-h-16 w-full min-w-0 resize-none rounded-[10px] border p-3 text-[16px] wrap-anywhere disabled:opacity-60"
        onChange={(event) => {
          setText(event.target.value);
          setProblem(null);
        }}
        onKeyDown={keys}
      />
      {problem ? (
        <p role="alert" className="text-danger text-sm">
          {problem}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-muted flex min-w-0 flex-col text-xs">
          <span id={`${field}-hint`}>{t("hint")}</span>
          <span id={`${field}-count`} className={cn(over && "text-danger font-medium")}>
            {over ? t("over", { over: count - BODY_MAX }) : t("count", { count, max: BODY_MAX })}
          </span>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="lg"
          disabled={closed || empty || over}
          aria-disabled={send.isPending}
        >
          <Send aria-hidden />
          {send.isPending ? t("sending") : problem ? t("retry") : t("send")}
        </Button>
      </div>
    </form>
  );
}
