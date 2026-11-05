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
} from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, type Ref, useLayoutEffect, useRef } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import {
  conversationMessagesList,
  conversationsRetrieve,
  getConversationMessagesListQueryKey,
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
// lectura: enviar, notas y asignar llegan en otros work items. Se monta con `key` = la conversación.
export function ChatPanel({
  conversation: row,
  headingRef,
  onBack,
}: {
  conversation: Conversation;
  headingRef: Ref<HTMLHeadingElement>;
  onBack: () => void;
}) {
  const t = useTranslations("inbox");
  const { organization } = useTenant();
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
  const scroller = useRef<HTMLDivElement>(null);
  const kept = useRef<number | null>(null); // lo que faltaba por desplazar al pedir los anteriores
  const placed = useRef(false);
  const list = useInfiniteQuery<PaginatedMessageList, ApiError>({
    queryKey: [...getConversationMessagesListQueryKey(slug, conversation.id), "pages"],
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
    else if (!placed.current && rows.length > 0) box.scrollTop = box.scrollHeight;
    placed.current = placed.current || rows.length > 0;
    kept.current = null;
  }, [rows.length, list.isFetchingNextPage]);

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
    <div className="border-border bg-surface flex h-[calc(100dvh-14rem)] min-h-80 flex-col rounded-lg border lg:h-[calc(100dvh-18rem)]">
      <header className="border-border flex flex-col gap-2 border-b p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="ghost"
            className="border-border min-h-11 border sm:min-h-9 lg:hidden"
            onClick={onBack}
          >
            <ArrowLeft aria-hidden />
            {t("back")}
          </Button>
          <Button
            variant="ghost"
            className="border-border ml-auto min-h-11 border sm:min-h-9"
            aria-disabled={list.isFetching}
            onClick={() => {
              if (list.isFetching) return;
              void list.refetch();
              void detail.refetch();
            }}
          >
            <RefreshCw aria-hidden />
            {t("chat.refresh")}
          </Button>
        </div>
        <h2
          ref={headingRef}
          tabIndex={-1}
          className="text-lg font-semibold focus-visible:outline-none"
        >
          {name === null ? t("contactUnavailable") : <Plain>{name}</Plain>}
        </h2>
        <p className="text-muted flex flex-wrap gap-x-2 text-sm">
          <span className="font-mono">{conversation.number}</span>
          <Plain>{conversation.channel.name}</Plain>
          <span>{t(`status.${conversation.status}`)}</span>
        </p>
        {stale ? (
          <p role="alert" className="text-danger text-sm">
            {stale}
          </p>
        ) : null}
        {name === null ? (
          <p className="text-muted text-sm">{t("noAccessContact")}</p>
        ) : (
          <Link
            href={`/o/${slug}/clientes/${conversation.contact.id}`}
            prefetch={false}
            className="self-start text-sm font-medium underline underline-offset-4"
          >
            {t("customerCard")}
          </Link>
        )}
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
