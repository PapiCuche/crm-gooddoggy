"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Check,
  CheckCheck,
  Clock,
  RefreshCw,
  StickyNote,
  TriangleAlert,
  UserRound,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, type Ref, useLayoutEffect, useRef } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import {
  conversationMessagesList,
  conversationsRetrieve,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type {
  Conversation,
  ConversationDetail,
  Message,
  PaginatedMessageList,
} from "@/lib/api/model";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { Assignee, AssignmentActions } from "./assignment";
import { Composer, messagesKey, writableModes } from "./composer";
import { Plain } from "./plain";

const WHEN = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } as const;
const MARKS: Record<string, ReactNode> = {
  QUEUED: <Clock aria-hidden />,
  SENT: <Check aria-hidden />,
  DELIVERED: <CheckCheck aria-hidden />,
  READ: <CheckCheck aria-hidden className="text-success" />,
  FAILED: <TriangleAlert aria-hidden />,
};

// El chat de una conversación (F6-03): los mensajes de `GET …/conversations/{id}/messages/`
// (F6-02c), de más antiguo a más reciente. La API los da del más nuevo al más antiguo, 50 por
// página; «Cargar mensajes anteriores» pide la página siguiente y deja la vista donde estaba. Solo
// lectura, más el cuadro de redacción (F6-04c, `composer.tsx`); notas y asignar llegan en otros work
// items. Se monta con `key` = la conversación.
export function ChatPanel({
  conversation: row,
  headingRef,
  onBack,
  onCustomer,
  visible,
}: {
  conversation: Conversation;
  headingRef: Ref<HTMLHeadingElement>;
  onBack: () => void;
  onCustomer: (contact: Conversation["contact"]) => void; // abre el panel del cliente (el chat sigue montado)
  visible: boolean; // el chat se oculta (no se desmonta) mientras se ve al cliente
}) {
  const t = useTranslations("inbox");
  const { organization, permissions, user } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  // La fila de la lista es una instantánea: el encabezado se alimenta del detalle (`GET …/{id}/`),
  // y mientras llega, o si falla, se queda con la fila.
  const detail = useQuery<ConversationDetail, ApiError>({
    queryKey: getConversationsRetrieveQueryKey(slug, row.id),
    queryFn: ({ signal }) => conversationsRetrieve(slug, row.id, { signal }),
    gcTime: 0,
  });
  const conversation = detail.data ?? row;
  const modes = writableModes(permissions, conversation, user.id);
  const scroller = useRef<HTMLDivElement>(null);
  const kept = useRef<number | null>(null); // lo que faltaba por desplazar al pedir los anteriores
  const placed = useRef(false);
  const atBottom = useRef(false); // al ocultarse el chat, ¿se leía lo último?
  const toBottom = useRef(false); // acabo de enviar: el mensaje nuevo se ve
  const list = useInfiniteQuery<PaginatedMessageList, ApiError>({
    queryKey: messagesKey(slug, conversation.id),
    queryFn: ({ pageParam, signal }) =>
      conversationMessagesList(
        slug,
        conversation.id,
        pageParam ? { cursor: pageParam as string } : undefined,
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    gcTime: 0,
  });
  // Cada página viene del más nuevo al más antiguo, y la primera es la más reciente.
  const rows = (list.data?.pages ?? []).toReversed().flatMap((page) => page.results.toReversed());
  const problem = failed(list, t("denied"));
  const stale = failed(detail, t("denied")); // el detalle no llegó: se sigue con lo que dio la lista

  useLayoutEffect(() => {
    const box = scroller.current;
    if (!box || list.isFetchingNextPage) return;
    if (kept.current !== null) box.scrollTop = box.scrollHeight - kept.current;
    else if (toBottom.current || (!placed.current && rows.length > 0)) {
      box.scrollTop = box.scrollHeight;
    }
    toBottom.current = false;
    placed.current = placed.current || rows.length > 0;
    kept.current = null;
  }, [rows.length, list.isFetchingNextPage]);

  useLayoutEffect(() => {
    const box = scroller.current;
    if (visible && atBottom.current && box) box.scrollTop = box.scrollHeight;
  }, [visible]);
  const name = conversation.contact.display_name;
  let body;
  if (!list.data && problem) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {problem}
        </p>
        {retry(list)}
      </div>
    );
  } else if (!list.data) {
    body = (
      <p role="status" className="text-muted">
        {t("chat.loading")}
      </p>
    );
  } else if (rows.length === 0) {
    body = <p className="text-muted">{t("chat.empty")}</p>;
  } else {
    body = (
      <ol aria-label={t("chat.label")} className="flex flex-col gap-3">
        {rows.map((message) => (
          <Bubble key={message.id} message={message} />
        ))}
      </ol>
    );
  }

  return (
    // Alto: en 390 px la pantalla pierde el título de la página (queda solo para lectores) y el panel
    // ocupa el viewport menos la barra y el relleno del shell (8 rem), con un mínimo (29 rem) que deja
    // sitio a las tres zonas; el encabezado y el cuadro no se comprimen y los mensajes (`min-h-0`)
    // absorben el sobrante y se desplazan. En escritorio, el alto de siempre.
    <div className="border-border bg-surface flex h-[calc(100dvh-8rem)] min-h-[29rem] flex-col rounded-lg border lg:h-[calc(100dvh-18rem)] lg:min-h-[26rem]">
      <header className="border-border relative flex shrink-0 flex-col gap-1 border-b p-2 lg:gap-2 lg:p-3">
        {/* Una sola fila que se parte en líneas. El DOM va en el orden de tabulación que se ve en
            escritorio (volver, nombre, actualizar, ficha) y `order` coloca cada pieza: en 320 px
            volver, nombre, actualizar y ficha (iconos) y debajo número, canal y estado; en escritorio
            actualizar a la derecha, el nombre, el número, el canal y el estado, y la ficha al final. */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Button
            variant="ghost"
            className="border-border order-1 size-10 border px-0 lg:hidden"
            onClick={onBack}
          >
            <ArrowLeft aria-hidden />
            <span className="sr-only">{t("back")}</span>
          </Button>
          <div className="order-2 min-w-0 flex-1 lg:order-3 lg:basis-full">
            <h2
              ref={headingRef}
              tabIndex={-1}
              className="line-clamp-1 text-base font-semibold focus-visible:outline-none lg:line-clamp-none lg:text-lg"
            >
              {name === null ? t("contactUnavailable") : <Plain>{name}</Plain>}
            </h2>
          </div>
          <Button
            variant="ghost"
            className="border-border order-3 border max-lg:size-10 max-lg:px-0 lg:order-2 lg:ml-auto"
            aria-disabled={list.isFetching}
            onClick={() => {
              if (list.isFetching) return;
              void list.refetch();
              void detail.refetch();
            }}
          >
            <RefreshCw aria-hidden />
            <span className="max-lg:sr-only">{t("chat.refresh")}</span>
          </Button>
          {name === null ? null : (
            <Button
              id="customer-toggle"
              variant="ghost"
              className="text-foreground border-border order-4 text-xs font-medium max-sm:size-10 max-sm:border max-sm:px-0 sm:h-auto sm:px-0 sm:text-sm sm:underline sm:underline-offset-4 lg:order-6 lg:self-start"
              aria-expanded="false" // al abrirse el panel, este botón se oculta con el chat
              aria-controls="customer-panel"
              onClick={() => {
                const box = scroller.current;
                // Oculto (`display: none`) el chat puede perder su posición: si se leía lo último, se vuelve a ello.
                atBottom.current = !box || box.scrollHeight - box.scrollTop - box.clientHeight < 8;
                onCustomer(conversation.contact);
              }}
            >
              <UserRound aria-hidden className="size-4 sm:hidden" />
              <span className="max-sm:sr-only">{t("customer.open")}</span>
            </Button>
          )}
          <p className="text-muted order-5 flex basis-full flex-wrap items-center gap-x-2 text-xs lg:text-sm">
            <span className="font-mono">{conversation.number}</span>
            <Plain>{conversation.channel.name}</Plain>
            <span>{t(`status.${conversation.status}`)}</span>
          </p>
          {name === null ? (
            <p className="text-muted order-5 basis-full text-xs lg:text-sm">
              {t("noAccessContact")}
            </p>
          ) : null}
        </div>
        {detail.data ? (
          <AssignmentActions detail={detail.data} />
        ) : (
          <p className="text-sm font-medium">
            <Assignee who={conversation} />
          </p>
        )}
        {stale ? (
          <p role="alert" className="text-danger text-sm">
            {stale}
          </p>
        ) : null}
      </header>
      {/* Una región que se desplaza debe poder enfocarse para recorrerla con el teclado. */}
      <div
        ref={scroller}
        role="region"
        tabIndex={0}
        aria-label={t("chat.scroll")}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3"
      >
        {list.hasNextPage ? (
          <Button
            variant="ghost"
            className="border-border min-h-11 self-center border sm:min-h-9"
            aria-disabled={list.isFetchingNextPage}
            onClick={() => {
              if (list.isFetchingNextPage) return;
              const box = scroller.current;
              kept.current = box ? box.scrollHeight - box.scrollTop : null;
              void list.fetchNextPage();
            }}
          >
            {list.isFetchingNextPage ? t("loadingMore") : t("chat.older")}
          </Button>
        ) : null}
        {list.data && problem ? (
          <p role="alert" className="text-danger text-sm">
            {problem}
          </p>
        ) : null}
        {body}
      </div>
      {modes.reply || modes.note ? (
        <Composer
          conversation={conversation}
          modes={modes}
          onQueued={() => (toBottom.current = true)}
        />
      ) : (
        <p className="border-border text-muted border-t p-3 text-sm">{t("composer.cannot")}</p>
      )}
    </div>
  );
}

function Bubble({ message }: { message: Message }) {
  const t = useTranslations("inbox");
  const format = useFormatter();
  // Una nota interna es otra cosa que un mensaje al cliente: va aparte, rotulada y nunca como uno.
  const note = message.message_type === "NOTE" || message.direction === "INTERNAL";
  const outbound = message.direction === "OUTBOUND";
  const state = outbound && message.delivery_status ? message.delivery_status : null;
  return (
    <li
      className={cn(
        "flex max-w-[85%] flex-col gap-1 rounded-[10px] border p-3 leading-snug",
        outbound && "bg-accent text-accent-foreground border-foreground/10 self-end",
        message.direction === "INBOUND" && "bg-background border-border self-start",
        note &&
          "bg-surface-raised border-foreground/40 w-full max-w-full self-stretch border-dashed",
      )}
    >
      {note ? (
        <p className="flex items-center gap-1.5 text-xs font-semibold uppercase">
          <StickyNote aria-hidden className="size-3.5" />
          {t("note")}
        </p>
      ) : null}
      {message.body === null ? (
        <p className="text-muted text-sm italic">{t("bodyHidden")}</p>
      ) : (
        <p>
          <Plain>{message.body}</Plain>
        </p>
      )}
      <p className="flex flex-wrap items-center gap-x-2 text-xs opacity-80">
        <span>{t(`sender.${message.sender_type}`)}</span>
        <time dateTime={message.created_at}>
          {format.dateTime(new Date(message.created_at), WHEN)}
        </time>
        {state && t.has(`delivery.${state}`) ? (
          <span
            className={cn(
              "inline-flex items-center gap-1 [&_svg]:size-3.5",
              state === "FAILED" && "text-danger font-semibold",
            )}
          >
            {MARKS[state]}
            {t(`delivery.${state}`)}
          </span>
        ) : null}
      </p>
    </li>
  );
}
