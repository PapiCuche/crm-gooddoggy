"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useState, useSyncExternalStore } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import {
  getConversationMessagesListQueryKey,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import { connectInbox, type InboxEvent, type SocketStatus } from "@/lib/realtime/inbox-socket";

import { notifyNewMessage } from "./attention";

// El tiempo real del Inbox (F6-07c): mientras la pantalla está abierta escucha
// `/ws/o/{slug}/inbox/` y, por cada evento, vuelve a pedir lo que cambió con las mismas lecturas de
// siempre (la API sigue siendo la fuente: el evento solo avisa). `message.*` relee los mensajes de
// esa conversación, su detalle y la lista; `conversation.updated`, el detalle y la lista. Los de una
// ráfaga se agrupan (UX-INBOX-06): una relectura por clave cada 300 ms como mucho. Tras una
// reconexión se relee todo lo del Inbox una vez, porque pudo perderse algo. Sin un WebSocket
// conectado todo sigue como antes (lectura al enfocar y botones «Actualizar»): no hay sondeo.
// Un mensaje entrante avisa en el navegador si quien atiende lo encendió (F6-09). Encendido en la
// imagen de producción (F6-09); `NEXT_PUBLIC_INBOX_WS=off` al construir lo apaga.
export const inboxRealtimeOn = () => process.env.NEXT_PUBLIC_INBOX_WS === "on";

const WINDOW = 300; // ms: una ráfaga de eventos se relee una vez por clave al final de la ventana

// UX-INBOX-06: lo que la pantalla dice del tiempo real. `off`, apagado (no se muestra nada).
export type LiveStatus = SocketStatus | "offline" | "off";
const onLine = () => navigator.onLine !== false;
const watchLine = (change: () => void) => {
  const off = new AbortController();
  window.addEventListener("online", change, { signal: off.signal });
  window.addEventListener("offline", change, { signal: off.signal });
  return () => off.abort();
};

export function useInboxRealtime(): LiveStatus {
  const client = useQueryClient();
  const slug = useTenant().organization.slug;
  const title = useTranslations("inbox.notify")("title");
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const networked = useSyncExternalStore(watchLine, onLine, () => true);
  useEffect(() => {
    if (!inboxRealtimeOn() || typeof WebSocket === "undefined") return;
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const mine = `/api/v1/o/${slug}/conversations/`;
    const due = new Map<string, QueryKey>();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const want = (queryKey: QueryKey) => due.set(JSON.stringify(queryKey), queryKey);
    // También con un envío propio en curso (UX-INBOX-06c): la relectura conserva su burbuja
    // `pending-…` (`keepPending` en el chat), así lo que escribe el cliente se ve mientras sale.
    const flush = () => {
      timer = undefined;
      for (const queryKey of due.values()) void client.invalidateQueries({ queryKey });
      due.clear();
    };
    const reread = (event: InboxEvent) => {
      const id = event.conversation_id;
      want(getConversationsListQueryKey(slug));
      want(getConversationsRetrieveQueryKey(slug, id));
      if (event.type !== "conversation.updated") {
        want(getConversationMessagesListQueryKey(slug, id));
      }
      timer ??= setTimeout(flush, WINDOW);
      if (event.type === "message.received") notifyNewMessage(title); // cada mensaje avisa, sin esperar
    };
    const stop = connectInbox({
      url: `${proto}//${window.location.host}/ws/o/${encodeURIComponent(slug)}/inbox/`,
      onEvent: reread,
      // Todo lo del Inbox de esta organización: la lista, los detalles y los mensajes.
      onResync: () => {
        due.clear(); // cubre lo pendiente de la ráfaga: no se relee otra vez al cerrar su ventana
        void client.invalidateQueries({
          predicate: ({ queryKey: [first] }) => typeof first === "string" && first.startsWith(mine),
        });
      },
      onStatus: setSocket, // parada (al salir u otra organización), ya no avisa: `connectInbox`
    });
    return () => {
      stop();
      clearTimeout(timer);
      due.clear();
    };
  }, [client, slug, title]);
  if (!inboxRealtimeOn() || typeof WebSocket === "undefined") return "off";
  if (!networked) return "offline";
  return socket;
}
