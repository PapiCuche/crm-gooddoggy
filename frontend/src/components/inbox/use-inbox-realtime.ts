"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import {
  getConversationMessagesListQueryKey,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import { connectInbox, type InboxEvent } from "@/lib/realtime/inbox-socket";

import { notifyNewMessage } from "./attention";

// El tiempo real del Inbox (F6-07c): mientras la pantalla está abierta escucha
// `/ws/o/{slug}/inbox/` y, por cada evento, vuelve a pedir lo que cambió con las mismas lecturas de
// siempre (la API sigue siendo la fuente: el evento solo avisa). `message.*` relee los mensajes de
// esa conversación, su detalle y la lista; `conversation.updated`, el detalle y la lista. Tras una
// reconexión se relee todo lo del Inbox una vez, porque pudo perderse algo. Sin un WebSocket
// conectado todo sigue como antes (lectura al enfocar y botones «Actualizar»): no hay sondeo.
// Un mensaje entrante avisa en el navegador si quien atiende lo encendió (F6-09). Encendido en la
// imagen de producción (F6-09); `NEXT_PUBLIC_INBOX_WS=off` al construir lo apaga.
export const inboxRealtimeOn = () => process.env.NEXT_PUBLIC_INBOX_WS === "on";

export function useInboxRealtime() {
  const client = useQueryClient();
  const slug = useTenant().organization.slug;
  const title = useTranslations("inbox.notify")("title");
  useEffect(() => {
    if (!inboxRealtimeOn() || typeof WebSocket === "undefined") return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const mine = `/api/v1/o/${slug}/conversations/`;
    const reread = (event: InboxEvent) => {
      const id = event.conversation_id;
      if (event.type === "message.received") notifyNewMessage(title);
      void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
      void client.invalidateQueries({ queryKey: getConversationsRetrieveQueryKey(slug, id) });
      if (event.type !== "conversation.updated") {
        void client.invalidateQueries({ queryKey: getConversationMessagesListQueryKey(slug, id) });
      }
    };
    return connectInbox({
      url: `${proto}//${window.location.host}/ws/o/${encodeURIComponent(slug)}/inbox/`,
      onEvent: reread,
      // Todo lo del Inbox de esta organización: la lista, los detalles y los mensajes.
      onResync: () =>
        void client.invalidateQueries({
          predicate: ({ queryKey: [first] }) => typeof first === "string" && first.startsWith(mine),
        }),
    });
  }, [client, slug, title]);
}
