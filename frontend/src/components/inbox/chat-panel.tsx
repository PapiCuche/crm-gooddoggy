"use client";

import { useInfiniteQuery, useMutationState, useQuery } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowLeft,
  AudioLines,
  Check,
  CheckCheck,
  Clock,
  Ellipsis,
  RefreshCw,
  FileQuestionMark,
  FileText,
  ImageIcon,
  StickyNote,
  TriangleAlert,
  UserRound,
  Video,
  WifiOff,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, type Ref, useEffect, useLayoutEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { ChannelSource, channelOf, useChannelName } from "@/components/channels/channel-identity";
import { ChannelMark } from "@/components/channels/channel-identity";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import {
  conversationMessagesList,
  conversationsRetrieve,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import {
  type Conversation,
  type ConversationDetail,
  type Message,
  type PaginatedMessageList,
  ProviderErrorCodeEnum,
} from "@/lib/api/model";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { useAiExclusion } from "./ai-exclusion";
import { Assignee, AssignmentActions } from "./assignment";
import {
  Composer,
  type Inserter,
  keepPending,
  messagesKey,
  pausedId,
  writableModes,
} from "./composer";
import { AI_USE, useCopilot } from "./copilot";
import { useHumanRequest } from "./human-request";
import { MediaFile } from "./media-file";
import { useComposing, useKeyboardHeight } from "./mobile";
import { Plain } from "./plain";

const WHEN = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" } as const;
const MARKS: Record<string, ReactNode> = {
  QUEUED: <Clock aria-hidden />,
  SENDING: <Clock aria-hidden />,
  SENT: <Check aria-hidden />,
  DELIVERED: <CheckCheck aria-hidden />,
  READ: <CheckCheck aria-hidden className="text-success" />,
  FAILED: <TriangleAlert aria-hidden />,
};
// F7b-17: dos rótulos más que no son estados del servidor. `RETRYING`, un `QUEUED` que el canal ya
// devolvió (`dispatch_attempts` > 0, p. ej. por un límite de envío); `UNCONFIRMED`, un `FAILED` con
// `PROVIDER_UNKNOWN` (pudo haber salido): ninguno dice «No se pudo enviar».
const LABELS: Record<string, ReactNode> = {
  RETRYING: <RefreshCw aria-hidden />,
  UNCONFIRMED: <Clock aria-hidden />,
};
const END = 8; // px: a menos de esto del final, se lee lo último
const still = () =>
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const PILL = 52; // px: el botón «N mensajes nuevos» (44) y su margen (8) tapan el pie de la región
// La última fila a la vista (y la siguiente) y su altura en la región: al releer no se mueve. Con
// `hidden`, a la vista por encima de esos px del pie (lo que asoma bajo el botón no se ha leído).
const lastSeen = (box: HTMLElement, hidden = 0) => {
  const view = box.getBoundingClientRect();
  const rows = [...box.querySelectorAll<HTMLElement>("li[data-id]")];
  const last = rows.findLastIndex((li) => li.getBoundingClientRect().top < view.bottom - hidden);
  return rows
    .slice(Math.max(last, 0), last + 2)
    .map((li) => ({ id: li.dataset.id!, top: li.getBoundingClientRect().top - view.top }));
};
// Al final (o yendo a él) se sigue lo último; si no, la fila leída vuelve a su altura en la región.
// Solo cambia `scrollTop`, nunca un alto: el `ResizeObserver` que la llama no se realimenta.
const place = (box: HTMLElement, follow: boolean, anchor: ReturnType<typeof lastSeen>) => {
  if (follow) return void (box.scrollTop = box.scrollHeight);
  const view = box.getBoundingClientRect().top;
  const [row, was] = anchor
    .map(({ id, top }) => [box.querySelector(`li[data-id="${CSS.escape(id)}"]`), top] as const)
    .find(([found]) => found) ?? [null, 0];
  if (row) box.scrollTop += row.getBoundingClientRect().top - view - was;
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
  // El copiloto solo inserta en «Responder»: sin `ai.use`, sin poder responder o cerrada, no se ofrece.
  // Excluida de la IA (F8-13), tampoco: ni «Sugerir» ni «Resumir» (el servidor ya lo rechaza).
  const usable =
    modes.reply &&
    conversation.status !== "CLOSED" &&
    permissions.some(({ code }) => code === AI_USE);
  const offered = usable && !conversation.ai_excluded_at;
  const inserter = useRef<Inserter | null>(null);
  const copilot = useCopilot({
    conversationId: conversation.id,
    onInsert: (text) => inserter.current?.(text),
  });
  const exclusion = useAiExclusion(conversation); // excluirla de la IA (F8-13b)
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null); // lo que se desplaza: su alto lo vigila un observador
  const height = useRef(0); // `scrollHeight` tras la última colocación
  const panel = useRef<HTMLDivElement>(null);
  useKeyboardHeight(panel, scroller);
  const [composing, release] = useComposing(panel); // UX-INBOX-03b: lo compacto existe solo entonces
  const human = useHumanRequest(conversation, composing); // con `conversations.view`, como ver el chat
  const refresh = useRef<HTMLButtonElement>(null);
  const kept = useRef<{ rest: number; pages: number } | null>(null); // al pedir los anteriores
  const placed = useRef(false);
  const atBottom = useRef(false); // al ocultarse el chat, ¿se leía lo último?
  const toBottom = useRef(false); // acabo de enviar: el mensaje nuevo se ve
  const pinned = useRef(true); // ¿se lee lo último? Se sigue; si no, se cuenta lo ajeno tras `seen`
  const anchor = useRef<ReturnType<typeof lastSeen>>([]);
  const chase = useRef({ on: false, top: 0 }); // el botón va al final: lo que llegue mientras, también
  const [atEnd, setAtEnd] = useState(true);
  const [seen, setSeen] = useState<string | null>(null);
  const key = messagesKey(slug, conversation.id);
  const list = useInfiniteQuery<PaginatedMessageList, ApiError>({
    queryKey: key,
    // La más reciente conserva la burbuja de un envío propio en curso (UX-INBOX-06c).
    queryFn: async ({ client, pageParam, signal }) => {
      const cursor = pageParam ? { cursor: pageParam as string } : undefined;
      const page = await conversationMessagesList(slug, conversation.id, cursor, { signal });
      return cursor ? page : keepPending(client, key, page);
    },
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    gcTime: 0,
  });
  // Cada página viene del más nuevo al más antiguo, y la primera es la más reciente.
  const rows = (list.data?.pages ?? []).toReversed().flatMap((page) => page.results.toReversed());
  const newest = rows.findLast(({ id }) => !id.startsWith("pending-"))?.id ?? null;
  const pages = list.data?.pages.length ?? 0;
  const after = rows.findIndex(({ id }) => id === seen);
  const unseen =
    atEnd || !seen
      ? 0
      : after < 0
        ? Infinity
        : rows.slice(after + 1).filter((message) => message.sender_user_id !== user.id).length;
  // Envíos en pausa sin conexión (TanStack los suelta al volver la red): su burbuja lo dice.
  const paused = useMutationState({ filters: { status: "pending" }, select: pausedId });
  const problem = failed(list, t("denied"));
  const stale = failed(detail, t("denied")); // el detalle no llegó: se sigue con lo que dio la lista

  useLayoutEffect(() => {
    const box = scroller.current;
    if (!box || list.isFetchingNextPage) return;
    if (kept.current && pages > kept.current.pages)
      box.scrollTop = box.scrollHeight - kept.current.rest;
    else {
      const follow = toBottom.current || pinned.current || chase.current.on || !placed.current;
      place(box, follow, anchor.current);
    }
    height.current = box.scrollHeight;
    toBottom.current = false;
    placed.current = placed.current || rows.length > 0;
    kept.current = null;
  }, [rows.length, newest, pages, list.isFetchingNextPage]);

  useLayoutEffect(() => {
    const box = scroller.current;
    if (visible && atBottom.current && box) box.scrollTop = box.scrollHeight;
  }, [visible]);

  // Un alto que cambia sin filas nuevas (el aviso de error, un adjunto que pasa a «Descargar», un
  // estado de entrega): con `overflow-anchor: none` nadie lo compensa. Lo ya colocado (mismo alto
  // que tras el efecto de arriba) no se vuelve a tocar.
  useEffect(() => {
    const [box, inner] = [scroller.current, content.current];
    if (!box || !inner || !window.ResizeObserver) return; // jsdom no lo trae
    const observer = new ResizeObserver(() => {
      if (box.scrollHeight === height.current) return;
      place(box, pinned.current || chase.current.on, anchor.current);
      height.current = box.scrollHeight;
    });
    observer.observe(inner);
    return () => observer.disconnect();
  }, []);
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
          <Bubble
            key={message.id}
            message={message}
            paused={message.id.startsWith("pending-") && paused.includes(message.client_message_id)}
            conversationId={conversation.id}
            platform={conversation.channel.platform}
          />
        ))}
      </ol>
    );
  }

  return (
    // Alto: en 390 px la pantalla pierde el título de la página (queda solo para lectores) y el panel
    // ocupa lo visible (`--keyboard-vh` en iOS) menos barra y relleno (8rem), con mínimo que deja
    // sitio a las tres zonas; el encabezado y el cuadro no se comprimen y los mensajes (`min-h-0`)
    // absorben el sobrante y se desplazan. En escritorio, el alto de siempre.
    <div
      ref={panel}
      data-composing={composing || undefined}
      className="group border-border bg-surface flex h-[calc(var(--keyboard-vh,100dvh)-8rem)] min-h-min flex-col rounded-lg border max-sm:data-composing:relative max-sm:data-composing:z-10 max-sm:data-composing:-mx-(--shell-gutter) max-sm:data-composing:-mt-[calc(var(--shell-bar)+var(--shell-pad))] max-sm:data-composing:-mb-(--shell-pad) max-sm:data-composing:h-[var(--keyboard-vh,100dvh)] max-sm:data-composing:rounded-none max-sm:data-composing:border-0 lg:h-[calc(100dvh-18rem)] lg:min-h-[26rem]"
    >
      <header className="border-border relative flex shrink-0 flex-col gap-1 border-b p-2 lg:gap-2 lg:p-3">
        {/* Una sola fila que se parte en líneas. El DOM va en el orden de tabulación que se ve en
            escritorio (volver, nombre, actualizar, ficha) y `order` coloca cada pieza: en 320 px
            volver, nombre, actualizar y ficha (iconos) y debajo número, canal y estado; en escritorio
            actualizar a la derecha, el nombre, el número, el canal y el estado, y la ficha al final. */}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Button
            variant="ghost"
            className="border-border order-1 size-11 border px-0 lg:hidden"
            onClick={onBack}
          >
            <ArrowLeft aria-hidden />
            <span className="sr-only">{t("back")}</span>
          </Button>
          <div className="order-2 min-w-0 flex-1 max-sm:min-w-14 lg:order-3 lg:basis-full">
            <h2
              ref={headingRef}
              tabIndex={-1}
              className="line-clamp-1 text-base font-semibold focus-visible:outline-none max-sm:group-data-composing:leading-tight lg:line-clamp-none lg:text-lg"
            >
              {name === null ? t("contactUnavailable") : <Plain>{name}</Plain>}
            </h2>
            {composing ? (
              <p className="hidden text-xs max-sm:flex">
                <ChannelMark platform={conversation.channel.platform} />
              </p>
            ) : null}
          </div>
          <Button
            variant="ghost"
            className="border-border order-4 hidden size-11 border px-0 max-sm:group-data-composing:inline-flex"
            onClick={() => (release(), refresh.current?.focus())}
          >
            <Ellipsis aria-hidden />
            <span className="sr-only">{t("chat.more")}</span>
          </Button>
          <Button
            ref={refresh}
            variant="ghost"
            className="border-border order-3 border max-lg:size-11 max-lg:px-0 max-sm:group-data-composing:hidden lg:order-2 lg:ml-auto"
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
          {human.button}
          {exclusion.button}
          {name === null ? null : (
            <Button
              id="customer-toggle"
              variant="ghost"
              className="text-foreground border-border order-4 text-xs font-medium max-sm:size-11 max-sm:border max-sm:px-0 max-sm:group-data-composing:hidden sm:h-auto sm:px-0 sm:text-sm sm:underline sm:underline-offset-4 sm:max-lg:min-h-11 lg:order-6 lg:self-start"
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
          <p className="text-muted order-5 flex basis-full flex-wrap items-center gap-x-2 text-xs max-sm:group-data-composing:hidden lg:text-sm">
            <span className="font-mono">{conversation.number}</span>
            <ChannelSource
              platform={conversation.channel.platform}
              account={conversation.channel.name}
            />
            <span>{t(`status.${conversation.status}`)}</span>
          </p>
          {name === null ? (
            <p className="text-muted order-5 basis-full text-xs max-sm:group-data-composing:hidden lg:text-sm">
              {t("noAccessContact")}
            </p>
          ) : null}
          {human.state}
          {exclusion.state}
        </div>
        {exclusion.dialog}
        {detail.data ? (
          <AssignmentActions detail={detail.data} />
        ) : (
          <p className="text-sm font-medium max-sm:group-data-composing:hidden">
            <Assignee who={conversation} />
          </p>
        )}
        {stale ? (
          <p role="alert" className="text-danger text-sm">
            {stale}
          </p>
        ) : null}
      </header>
      {/* El aviso de «N mensajes nuevos» va antes que la región en el DOM, por encima de su pie: con
          teclado se llega a él sin pasar por cada «Guardar» o «Reintentar» del chat. */}
      <div className="relative flex min-h-0 flex-1 flex-col">
        <div
          role="status"
          className="pointer-events-none absolute inset-x-0 bottom-2 z-10 flex justify-center"
        >
          {unseen > 0 ? (
            <Button
              variant="primary"
              className="pointer-events-auto h-11 rounded-full px-4 shadow-md transition-[opacity,translate,scale] duration-200 starting:translate-y-2 starting:opacity-0 motion-reduce:starting:translate-y-0"
              onClick={() => {
                const box = scroller.current;
                if (!box) return;
                chase.current.on = true;
                // De lejos, un salto hasta dos pantallas del final y el resto suave (corto, no segundos).
                if (!still())
                  box.scrollTop = Math.max(box.scrollTop, box.scrollHeight - 2 * box.clientHeight);
                box.scrollTo({ top: box.scrollHeight, behavior: still() ? "auto" : "smooth" });
                box.focus({ preventScroll: true }); // el botón se va: el foco no se pierde
              }}
            >
              <ArrowDown aria-hidden />
              {unseen === Infinity ? t("chat.unseenMany") : t("chat.unseen", { count: unseen })}
            </Button>
          ) : null}
        </div>
        {/* Una región que se desplaza debe poder enfocarse para recorrerla con el teclado. Con el
            botón a la vista, un control enfocado no queda debajo de él (`scroll-padding`). */}
        <div
          ref={scroller}
          role="region"
          tabIndex={0}
          aria-label={t("chat.scroll")}
          className={cn(
            "flex min-h-0 flex-1 flex-col overflow-y-auto p-3 contain-size [overflow-anchor:none]",
            unseen > 0 && "scroll-pb-16",
          )}
          onWheel={() => void (chase.current.on = false)} // quien lee toma el control: no se persigue
          onTouchStart={() => void (chase.current.on = false)}
          onPointerDown={() => void (chase.current.on = false)} // la barra
          onScroll={({ currentTarget: box }) => {
            // `seen`, al dejar el final (seguir lo nuevo no siempre da `scroll`) y luego hasta la
            // última fila a la vista (sin la burbuja pendiente): solo cuenta lo que queda debajo.
            const was = pinned.current;
            pinned.current = box.scrollHeight - box.scrollTop - box.clientHeight < END;
            if (pinned.current || box.scrollTop < chase.current.top) chase.current.on = false;
            chase.current.top = box.scrollTop;
            anchor.current = lastSeen(box);
            const read = lastSeen(box, PILL)[0]?.id;
            const shown = rows.findIndex(({ id }) => id === read);
            const upto = rows.findLastIndex(
              (row, i) => i <= shown && !row.id.startsWith("pending-"),
            );
            setAtEnd(pinned.current);
            if (was) setSeen(newest);
            else if (upto > after) setSeen(rows[upto]!.id);
          }}
        >
          <div ref={content} className="flex flex-col gap-3">
            {list.hasNextPage ? (
              <Button
                variant="ghost"
                className="border-border min-h-11 self-center border lg:min-h-9"
                aria-disabled={list.isFetchingNextPage}
                onClick={() => {
                  if (list.isFetchingNextPage) return;
                  const box = scroller.current;
                  kept.current = box && { rest: box.scrollHeight - box.scrollTop, pages };
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
      </div>
      {offered ? copilot.panel : null}
      {usable && !offered ? (
        <p role="note" className="border-border text-muted border-t px-3 py-2 text-sm">
          {t("copilot.excluded")}
        </p>
      ) : null}
      {/* El redactor va el último del panel: `useComposing` lo reconoce así (nada después). */}
      {modes.reply || modes.note ? (
        <Composer
          inserter={inserter}
          tools={
            offered ? (
              <>
                {copilot.summarizer}
                {copilot.trigger}
              </>
            ) : null
          }
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

const CODES: readonly string[] = Object.values(ProviderErrorCodeEnum);
const META_ERROR = /^[0-9]{1,10}(\/[0-9]{1,10})?$/; // como `messages_error_detail_ck`
// `delivery_status` es un texto libre en el servidor: solo un estado conocido llega a `t()` y a
// `MARKS` (`t.has` dice que sí de claves del prototipo como `constructor`).
const STATES: readonly string[] = Object.keys(MARKS);
// Un medio (F7-09a2b): su clase con un icono y un rótulo fijos, y debajo su archivo si es entrante
// (`MediaFile`, F7-09a3). Un documento solo sale (F7-09b3). `Object.hasOwn`: ni `constructor` ni otra
// clave del prototipo.
const MEDIA: Record<string, ReactNode> = {
  IMAGE: <ImageIcon aria-hidden className="size-3.5" />,
  AUDIO: <AudioLines aria-hidden className="size-3.5" />,
  VIDEO: <Video aria-hidden className="size-3.5" />,
  DOCUMENT: <FileText aria-hidden className="size-3.5" />,
};

function Bubble({
  message,
  paused,
  conversationId,
  platform,
}: {
  message: Message;
  paused: boolean; // un envío propio sin conexión: sale al volver la red
  conversationId: string;
  platform: string;
}) {
  const t = useTranslations("inbox");
  const format = useFormatter();
  const network = useChannelName();
  // Una nota interna es otra cosa que un mensaje al cliente: va aparte, rotulada y nunca como uno.
  const note = message.message_type === "NOTE" || message.direction === "INTERNAL";
  // Un medio o un entrante que el canal aún no entiende: su propio marcador con el texto fijo. El
  // cuerpo, si llega, es una frase fija del servidor («Imagen recibida»), nunca el contenido. Un
  // tipo que esta pantalla no conoce es «no soportado», nunca un texto.
  const type = message.message_type;
  const media = !note && Object.hasOwn(MEDIA, type) ? type : null;
  const unsupported = !note && !media && type !== "TEXT";
  const outbound = message.direction === "OUTBOUND";
  // El cuerpo de un medio saliente es el pie de foto de quien lo envió (un texto suyo) o, sin él, la
  // misma etiqueta que el rótulo, que no se repite. El de un entrante es una frase fija.
  const label = media && t(`media.${message.attachment?.kind === "STICKER" ? "STICKER" : media}`);
  const fixed = unsupported || (media && !outbound);
  const state = outbound && message.delivery_status ? message.delivery_status : null;
  // El motivo de un fallo sale de una tabla de textos por código cerrado; un código que la tabla
  // no conoce (o ninguno) no muestra nada más que «No se pudo enviar»: nunca el valor crudo.
  const code = message.provider_error_code;
  // `t.has` también dice que sí de `constructor` o `toString` (claves del prototipo): se exige
  // además que el código sea uno del conjunto cerrado del contrato.
  const reason =
    state === "FAILED" && code && CODES.includes(code) && t.has(`failure.${code}`) ? code : null;
  // «Puede que sí se haya enviado» no es un fallo seguro: tono neutro, no el rojo de un error.
  const unsure = reason === "PROVIDER_UNKNOWN";
  // Los textos de `failure` que nombran WhatsApp tienen su versión para Instagram y Messenger
  // (`failureSocial`, F7b-06f), con la red de la conversación; las demás plataformas, los de siempre.
  // La red la nombra el mapa cerrado de `channel-identity` (`social`); nada sale del servidor.
  // F7b-19: un rechazo pasajero tardío (F7b-18) no se reintentó (`dispatch_attempts` = 0): sin «tras
  // varios intentos». Sin el campo (la API anterior), el texto de siempre.
  const why = !reason
    ? null
    : reason === "RETRIES_EXHAUSTED" && message.dispatch_attempts === 0
      ? t("failureOnce")
      : channelOf(platform).social && t.has(`failureSocial.${reason}`)
        ? t(`failureSocial.${reason}`, { network: network(platform) })
        : t(`failure.${reason}`);
  // El error de Meta en números (F7b-13), junto al motivo: solo con la forma `código[/subcódigo]`,
  // y una pista corta si es uno conocido. Nunca otro texto del servidor.
  const raw = message.provider_error_detail;
  const detail = state === "FAILED" && raw && META_ERROR.test(raw) ? raw : null;
  // Sin pista para `código/subcódigo` (p. ej. `10/2018065`), la del código solo (`10`), F7b-17. Solo
  // en un rechazo genérico: con otro motivo el subcódigo ya dijo otra cosa (`10/2018278` es la
  // ventana cerrada, `200/2534041` los mensajes apagados) y la pista del código lo contradiría.
  const bare = reason === "PROVIDER_REJECTED" ? detail?.split("/")[0] : null;
  const known = [detail, bare].find((key) => key && t.has(`failureMeta.${key}`));
  const hint = known ? t(`failureMeta.${known}`) : null;
  const meta = !detail
    ? null
    : hint
      ? t("failureCodeHint", { code: detail, hint })
      : t("failureCode", { code: detail });
  const retrying = state === "QUEUED" && message.dispatch_attempts > 0;
  const shown = retrying ? "RETRYING" : unsure ? "UNCONFIRMED" : state;
  return (
    <li
      data-id={message.id}
      className={cn(
        "flex max-w-[85%] flex-col gap-1 rounded-[10px] border p-3 leading-snug",
        outbound && "bg-accent text-accent-foreground border-foreground/10 self-end",
        message.direction === "INBOUND" && "bg-background border-border self-start",
        fixed && "bg-surface-raised",
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
      {unsupported ? (
        <p className="text-muted flex items-center gap-1.5 text-xs font-semibold uppercase">
          <FileQuestionMark aria-hidden className="size-3.5" />
          {t("unsupported")}
        </p>
      ) : null}
      {media ? (
        <p className="text-muted flex items-center gap-1.5 text-xs font-semibold uppercase">
          {MEDIA[media]}
          {label}
        </p>
      ) : null}
      {message.body === null ? (
        unsupported || media ? null : (
          <p className="text-muted text-sm italic">{t("bodyHidden")}</p>
        )
      ) : outbound && media && message.body === label ? null : (
        <p className={fixed ? "text-muted text-sm italic" : undefined}>
          <Plain>{message.body}</Plain>
        </p>
      )}
      {/* Sin `contacts.view` (cuerpo `null`) el contenido no se ve: tampoco el estado del archivo. */}
      {media && message.attachment && message.body !== null ? (
        <MediaFile conversationId={conversationId} attachment={message.attachment} />
      ) : null}
      <p className="flex flex-wrap items-center gap-x-2 text-xs opacity-80">
        <span>{t(`sender.${message.sender_type}`)}</span>
        <time dateTime={message.created_at}>
          {format.dateTime(new Date(message.created_at), WHEN)}
        </time>
        {paused ? (
          <span className="inline-flex items-center gap-1 [&_svg]:size-3.5">
            <WifiOff aria-hidden />
            {t(note ? "pausedNote" : "paused")}
          </span>
        ) : shown && state && STATES.includes(state) && t.has(`delivery.${shown}`) ? (
          <span
            className={cn(
              "inline-flex items-center gap-1 [&_svg]:size-3.5",
              shown === "FAILED" && "text-danger font-semibold",
            )}
          >
            {LABELS[shown] ?? MARKS[state]}
            {t(`delivery.${shown}`)}
          </span>
        ) : null}
      </p>
      {why || meta ? (
        <p className={cn("text-xs", unsure ? "text-foreground" : "text-danger")}>
          {[why, meta].filter(Boolean).join(" ")}
        </p>
      ) : null}
    </li>
  );
}
