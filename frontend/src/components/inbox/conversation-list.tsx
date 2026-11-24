"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { BotOff, CircleAlert, Hand, RefreshCw } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { ChannelDot, ChannelMark } from "@/components/channels/channel-identity";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import { conversationsList, getConversationsListQueryKey } from "@/lib/api/client";
import type {
  Conversation,
  ConversationsListParams,
  PaginatedConversationList,
} from "@/lib/api/model";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { Assignee } from "./assignment";
import { useUnanswered } from "./attention";
import { Plain } from "./plain";

export const BOXES = ["all", "mine", "unassigned"] as const;
export const STATUSES = [
  ...["", "OPEN", "PENDING", "WAITING_CUSTOMER"],
  ...["WAITING_INTERNAL", "RESOLVED", "CLOSED"],
] as const;
const SHORT = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } as const;
export type Filters = {
  box: (typeof BOXES)[number];
  status: (typeof STATUSES)[number];
  unanswered: boolean; // solo las abiertas cuyo último mensaje es del cliente (F6-09)
};

// La lista de conversaciones (F6-03): las que devuelve `GET …/conversations/` (F6-02b), la de
// mensaje más reciente primero, con la bandeja y el estado como filtros. Quién ve qué lo decide
// la API (`conversations.view` y su alcance); un 403 solo se explica. Un fallo al cargar más o al
// actualizar no borra lo que ya se muestra: se avisa debajo y el botón reintenta.
export function ConversationList({
  filters,
  onFilters,
  selectedId,
  onSelect,
}: {
  filters: Filters;
  onFilters: (filters: Filters) => void;
  selectedId: string | null;
  onSelect: (conversation: Conversation) => void;
}) {
  const t = useTranslations("inbox");
  const { organization } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const params: ConversationsListParams = {
    ...(filters.box === "all" ? {} : { box: filters.box }),
    ...(filters.status ? { status: filters.status } : {}),
    ...(filters.unanswered ? { unanswered: true } : {}),
  };
  const waiting = useUnanswered(filters.box);
  // Cada filtro es su propia lista; los filtros viajan también con «Cargar más».
  const list = useInfiniteQuery<PaginatedConversationList, ApiError>({
    queryKey: [...getConversationsListQueryKey(slug, params), "pages"],
    queryFn: ({ pageParam, signal }) =>
      conversationsList(
        slug,
        { ...params, ...(pageParam ? { cursor: pageParam as string } : {}) },
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    gcTime: 0,
  });
  const rows = list.data?.pages.flatMap((page) => page.results) ?? [];
  const problem = failed(list, t("denied"));

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
        {t("loading")}
      </p>
    );
  } else if (rows.length === 0) {
    body = (
      <p className="text-muted">
        {filters.box === "all" && !filters.status && !filters.unanswered
          ? t("empty")
          : t("emptyFiltered")}
      </p>
    );
  } else {
    body = (
      <ul aria-label={t("list.label")} className="flex flex-col gap-2">
        {rows.map((row) => (
          <Row
            key={row.id}
            row={row}
            selected={row.id === selectedId}
            onSelect={() => onSelect(row)}
          />
        ))}
      </ul>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div role="group" aria-label={t("filters.landmark")} className="flex flex-col gap-3">
        <div role="group" aria-label={t("filters.box")} className="flex flex-wrap gap-2">
          {BOXES.map((box) => (
            <Button
              key={box}
              variant={filters.box === box ? "accent" : "ghost"}
              className="border-border min-h-11 border sm:min-h-9"
              aria-pressed={filters.box === box}
              onClick={() => onFilters({ ...filters, box })}
            >
              {t(`filters.boxes.${box}`)}
            </Button>
          ))}
          <Button
            variant={filters.unanswered ? "accent" : "ghost"}
            className="border-border min-h-11 border sm:min-h-9"
            aria-pressed={filters.unanswered}
            onClick={() => onFilters({ ...filters, unanswered: !filters.unanswered })}
          >
            {t("unanswered.filter")}
            {waiting === undefined ? null : (
              <>
                <span aria-hidden className="bg-danger text-surface rounded-full px-2 text-xs">
                  {waiting}
                </span>
                <span className="sr-only">, {t("unanswered.count", { count: waiting })}</span>
              </>
            )}
          </Button>
        </div>
        <Choice
          label={t("filters.status")}
          value={filters.status}
          busy={false}
          onChange={(status) => onFilters({ ...filters, status: status as Filters["status"] })}
          options={STATUSES.map((one): [string, string] => [
            one,
            one ? t(`status.${one}`) : t("filters.anyStatus"),
          ])}
        />
        <Button
          variant="ghost"
          className="border-border min-h-11 self-start border sm:min-h-9"
          aria-disabled={list.isFetching}
          onClick={() => list.isFetching || void list.refetch()}
        >
          <RefreshCw aria-hidden />
          {list.isRefetching ? t("refreshing") : t("refresh")}
        </Button>
      </div>
      {list.data ? (
        <p role="status" className="sr-only">
          {t("count", { count: rows.length })}
        </p>
      ) : null}
      {body}
      {list.data && problem ? (
        <p role="alert" className="text-danger text-sm">
          {problem}
        </p>
      ) : null}
      {list.hasNextPage ? (
        <Button
          variant="accent"
          size="lg"
          className="self-start"
          aria-disabled={list.isFetchingNextPage}
          onClick={() => list.isFetchingNextPage || void list.fetchNextPage()}
        >
          {list.isFetchingNextPage ? t("loadingMore") : t("more")}
        </Button>
      ) : null}
    </div>
  );
}

function Row({
  row,
  selected,
  onSelect,
}: {
  row: Conversation;
  selected: boolean;
  onSelect: () => void;
}) {
  const t = useTranslations("inbox");
  const format = useFormatter();
  const name = row.contact.display_name;
  // Sin nombre, la API tampoco da la vista previa: se explica en lugar de dejar un hueco.
  let preview = <span className="italic">{t("noAccess")}</span>;
  if (name !== null && row.last_message_preview !== null) {
    preview = (
      <>
        {row.last_message_direction === "OUTBOUND" ? <span>{t("you")} </span> : null}
        <Plain>{row.last_message_preview}</Plain>
      </>
    );
  } else if (name !== null) {
    preview = <span className="italic">{t("noMessages")}</span>;
  }
  return (
    <li>
      <button
        type="button"
        id={`conversation-${row.id}`}
        aria-current={selected ? "true" : undefined}
        onClick={onSelect}
        className={cn(
          "border-border bg-surface hover:bg-surface-raised flex min-h-11 w-full flex-col gap-1 rounded-[10px] border p-3 text-left leading-snug transition-colors duration-150",
          selected && "border-foreground bg-accent/30 border-l-4",
        )}
      >
        <span className="flex items-start justify-between gap-2">
          {/* El punto de la red (UX-INBOX-01) se suma al nombre de la red de abajo; la elegida sigue
              marcada por su borde y su fondo, y los avisos por su icono y su texto. */}
          <span className="flex min-w-0 items-start gap-2">
            <ChannelDot platform={row.channel.platform} className="mt-1.5" />
            <span className="min-w-0 font-medium">
              {name === null ? t("contactUnavailable") : <Plain>{name}</Plain>}
            </span>
          </span>
          <time dateTime={row.last_message_at} className="text-muted shrink-0 text-xs">
            {format.dateTime(new Date(row.last_message_at), SHORT)}
          </time>
        </span>
        <span className="text-muted line-clamp-2 text-sm">{preview}</span>
        <span className="text-muted flex flex-wrap gap-x-2 text-xs">
          <span className="font-mono">{row.number}</span>
          <ChannelMark platform={row.channel.platform} />
          <Plain>{row.channel.name}</Plain>
          <span>{t(`status.${row.status}`)}</span>
          <span>
            <Assignee who={row} />
          </span>
          {row.last_reply_failed ? (
            // F7b-14: lo último es nuestro, pero no le llegó: sigue sin responder
            <span className="text-danger inline-flex items-center gap-1 font-semibold">
              <CircleAlert aria-hidden className="size-3" />
              {t("replyFailed")}
            </span>
          ) : null}
          {row.human_request ? (
            <span className="text-foreground inline-flex items-center gap-1 font-semibold">
              <Hand aria-hidden className="size-3" />
              {t("humanRequest.badge")}
            </span>
          ) : null}
          {row.ai_excluded_at ? (
            <span className="inline-flex items-center gap-1">
              <BotOff aria-hidden className="size-3" />
              {t("aiExclusion.mark")}
            </span>
          ) : null}
        </span>
      </button>
    </li>
  );
}
