"use client";

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { RotateCw, Send, StickyNote } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationMessagesSend,
  conversationNotesCreate,
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
// Un 409 de la política de envío: lo que cambió puede estar también en la conversación.
const REFRESH_ON = [
  "CONVERSATION_CLOSED",
  "CHANNEL_UNAVAILABLE",
  "CONTACT_BLOCKED",
  "WINDOW_CLOSED",
];
type Pages = InfiniteData<PaginatedMessageList, string>;

export const messagesKey = (slug: string, id: string) =>
  [...getConversationMessagesListQueryKey(slug, id), "pages"] as const;

export type Mode = "reply" | "note";
const CODES = { reply: "messages.send", note: "messages.note" } as const;

// ¿Qué modos se ofrecen? Es comodidad, no seguridad: la API decide en cada envío. Sin alcance
// concedido no se ofrece (como ella, que sin alcance no deja nada); con alcance solo `OWN` basta con
// que la conversación sea de quien escribe; con equipo o sucursal no se sabe desde aquí y se
// ofrece (si la API responde 404, se explica). Responder y anotar son permisos distintos.
export function writableModes(
  grants: readonly Grant[],
  conversation: Conversation,
  userId: string,
) {
  const allowed = (mode: Mode) => {
    const grant = grants.find(({ code }) => code === CODES[mode]);
    if (!grant || grant.scopes.length === 0) return false;
    if (grant.scopes.some((scope) => scope !== "OWN")) return true;
    return conversation.assigned_user_id === userId;
  };
  return { reply: allowed("reply"), note: allowed("note") };
}

const uuid = () => crypto.randomUUID();

// Cambia los mensajes de cada página de la caché (la primera página es la más reciente).
const edit = (data: Pages | undefined, change: (rows: Message[], newest: boolean) => Message[]) =>
  data && {
    ...data,
    pages: data.pages.map((page, i) => ({ ...page, results: change(page.results, i === 0) })),
  };

// El cuadro de redacción (F6-04c y F6-05c): dos modos con su propio borrador, su propia ruta y su
// propio identificador. «Responder al cliente» (`POST …/messages/`, `messages.send`) y «Nota
// interna» (`POST …/notes/`, `messages.note`). Siempre arranca en «Responder» si se puede, para no
// escribir una nota creyendo que es una respuesta; con un solo modo no hay conmutador. Los dos
// formularios quedan montados y el inactivo se oculta, así cada borrador, cada intento y cada error
// sobreviven al cambio de modo.
export function Composer({
  conversation,
  modes,
  onQueued,
}: {
  conversation: Conversation;
  modes: { reply: boolean; note: boolean };
  onQueued: () => void;
}) {
  const t = useTranslations("inbox.composer");
  const [chosen, setChosen] = useState<Mode>(modes.reply ? "reply" : "note");
  const shown: Mode = modes[chosen] ? chosen : modes.reply ? "reply" : "note";
  const both = modes.reply && modes.note;
  return (
    <div className="border-border flex min-w-0 shrink-0 flex-col gap-2 border-t p-2 lg:p-3">
      {both ? (
        <div role="group" aria-label={t("mode.label")} className="flex gap-2">
          {(["reply", "note"] as const).map((mode) => (
            <Button
              key={mode}
              type="button"
              variant={shown === mode ? "accent" : "ghost"}
              className="border-border h-9 min-w-0 border max-sm:flex-1"
              aria-pressed={shown === mode}
              aria-label={t(`mode.${mode}`)}
              onClick={() => setChosen(mode)}
            >
              {mode === "note" ? <StickyNote aria-hidden /> : null}
              {/* En 320 px las dos etiquetas largas no caben en una fila: se acortan, el nombre accesible no. */}
              <span className="max-sm:hidden">{t(`mode.${mode}`)}</span>
              <span className="sm:hidden">{t(`mode.${mode}Short`)}</span>
            </Button>
          ))}
        </div>
      ) : null}
      {(["reply", "note"] as const).map((mode) =>
        modes[mode] ? (
          <ComposeBox
            key={mode}
            mode={mode}
            active={shown === mode}
            tabbed={both}
            conversation={conversation}
            onQueued={onQueued}
          />
        ) : null,
      )}
    </div>
  );
}

function ComposeBox({
  mode,
  active,
  tabbed,
  conversation,
  onQueued,
}: {
  mode: Mode;
  active: boolean;
  tabbed: boolean; // hay conmutador: su botón ya dice el modo, la etiqueta queda para lectores
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
  const [saved, setSaved] = useState<string | null>(null);
  const note = mode === "note";
  const key = messagesKey(slug, conversation.id);
  const count = Array.from(text).length;
  // Una nota se admite también en una conversación cerrada; una respuesta no.
  const closed = !note && conversation.status === "CLOSED";
  const over = count > BODY_MAX;
  const empty = text.trim() === "";

  const send = useMutation<Message, ApiError, SendMessageRequest>({
    mutationFn: (request) =>
      note
        ? conversationNotesCreate(slug, conversation.id, request)
        : conversationMessagesSend(slug, conversation.id, request),
    onMutate: async (request) => {
      await client.cancelQueries({ queryKey: key });
      onQueued();
      const pending: Message = {
        ...{ id: `pending-${request.client_message_id}`, sender_type: "USER" },
        ...{ direction: note ? "INTERNAL" : "OUTBOUND", message_type: note ? "NOTE" : "TEXT" },
        ...{ sender_user_id: user.id, body: request.body },
        ...{ client_message_id: request.client_message_id },
        ...{ delivery_status: note ? null : "QUEUED", created_at: new Date().toISOString() },
        ...{ sent_at: null, delivered_at: null, read_at: null, failed_at: null },
        provider_error_code: null,
      };
      client.setQueryData<Pages>(key, (data) =>
        edit(data, (rows, newest) => (newest ? [pending, ...rows] : rows)),
      );
      setProblem(null);
      setSaved(null);
    },
    onSuccess: (done, request) => {
      attempt.current = null; // un id por acción: el siguiente texto lleva otro
      setText("");
      const mine = `pending-${request.client_message_id}`;
      client.setQueryData<Pages>(key, (data) => {
        const known = data?.pages.some((page) => page.results.some(({ id }) => id === done.id));
        return edit(data, (rows, newest) => [
          ...(newest && !known ? [done] : []),
          ...rows.filter(({ id }) => id !== mine),
        ]);
      });
      // Quien anota sin ver al contacto recibe el texto `null`: se explica en lugar de callarlo.
      if (note) setSaved(done.body === null ? t("savedHidden") : t("saved"));
      // El estado de entrega avanza en el servidor: se vuelve a pedir; la lista cambia su vista previa.
      void client.invalidateQueries({ queryKey: key });
      if (!note) void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
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
      if (REFRESH_ON.includes(error.code)) {
        void client.invalidateQueries({
          queryKey: getConversationsRetrieveQueryKey(slug, conversation.id),
        });
      }
      setProblem(
        error.status === 403 || error.status === 404
          ? t(note ? "deniedNote" : "denied")
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
    // Enter envía; Mayús+Enter agrega una línea. Durante una composición (IME) Enter es del IME;
    // Safari lo entrega ya sin `isComposing`, pero con `keyCode` 229.
    const composing = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
    if (event.key !== "Enter" || event.shiftKey || composing) return;
    event.preventDefault();
    submit();
  };
  const idle = note ? t("save") : t("send");
  // En menos de 640 px el formulario es una cuadrícula de dos columnas: la respuesta lleva el cuadro
  // y un botón de icono en la misma fila; la nota, el cuadro y debajo su aviso con «Guardar nota».
  const first = tabbed ? "max-sm:row-start-1" : "max-sm:row-start-2";
  const second = tabbed ? "max-sm:row-start-2" : "max-sm:row-start-3";

  return (
    <form
      hidden={!active}
      className={cn(
        "flex min-w-0 flex-col gap-2 max-sm:grid max-sm:grid-cols-[minmax(0,1fr)_auto] max-sm:gap-x-2 max-sm:gap-y-1",
        note &&
          "bg-surface-raised border-foreground/40 rounded-[10px] border border-dashed p-2 lg:p-3",
      )}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label
        htmlFor={field}
        className={cn(
          "text-sm font-medium max-sm:col-span-2 max-sm:row-start-1",
          tabbed && "max-lg:sr-only",
        )}
      >
        {t(`label.${mode}`)}
      </label>
      {/* 16 px: por debajo, Safari en iOS amplía la página al enfocar. */}
      <textarea
        id={field}
        ref={box}
        rows={1}
        value={text}
        disabled={closed}
        readOnly={send.isPending}
        aria-describedby={`${field}-count ${field}-keys${note ? ` ${field}-hint` : ""}${closed ? ` ${field}-closed` : ""}`}
        aria-invalid={over || problem ? true : undefined}
        className={cn(
          "bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger field-sizing-content max-h-24 min-h-10 w-full min-w-0 resize-none rounded-[10px] border px-3 py-2 text-[16px] wrap-anywhere disabled:opacity-60 lg:field-sizing-fixed lg:max-h-40 lg:min-h-16 lg:p-3",
          note && "border-dashed",
          note ? "max-sm:col-span-2" : "max-sm:col-start-1",
          first,
        )}
        onChange={(event) => {
          setText(event.target.value);
          setProblem(null);
          setSaved(null);
        }}
        onKeyDown={keys}
      />
      <p role="status" className="sr-only">
        {send.isPending ? t(note ? "saving" : "sending") : ""}
      </p>
      {/* El tope se anuncia solo al cruzar el 90 % y el 100 %: el texto depende del nivel, no de la cuenta. */}
      <p aria-live="polite" className="sr-only">
        {over
          ? t(note ? "overLimit.note" : "overLimit.reply")
          : count >= BODY_MAX * 0.9
            ? t("nearLimit")
            : ""}
      </p>
      <div className="flex flex-wrap items-center justify-between gap-2 max-sm:contents">
        <div
          className={cn(
            "text-muted flex min-w-0 flex-col text-xs",
            note ? "max-sm:col-start-1" : "max-sm:col-span-2",
            second,
          )}
        >
          {/* El aviso de la nota se ve siempre; en 390 px no hay tecla Enter que explicar y la ayuda de teclas queda para lectores. */}
          {note ? (
            <span id={`${field}-hint`} className="text-foreground font-medium">
              {t("hint.note")}
            </span>
          ) : null}
          <span id={`${field}-keys`} className="max-lg:sr-only">
            {t(note ? "hint.noteKeys" : "hint.reply")}
          </span>
          <span
            id={`${field}-count`}
            className={cn(
              over && "text-danger font-medium",
              !over && count < BODY_MAX * 0.9 && "max-lg:sr-only",
            )}
          >
            {over ? t("over", { over: count - BODY_MAX }) : t("count", { count, max: BODY_MAX })}
          </span>
        </div>
        <Button
          type="submit"
          variant="primary"
          size="lg"
          className={cn(
            "max-lg:h-10 max-sm:col-start-2 max-sm:self-center",
            note ? second : cn("max-sm:size-10 max-sm:px-0", first),
          )}
          disabled={closed || empty || over}
          aria-disabled={send.isPending}
        >
          {note ? (
            <StickyNote aria-hidden />
          ) : problem ? (
            <RotateCw aria-hidden />
          ) : (
            <Send aria-hidden />
          )}
          <span className={note ? undefined : "max-sm:sr-only"}>
            {send.isPending ? t(note ? "saving" : "sending") : problem ? t("retry") : idle}
          </span>
        </Button>
      </div>
      {closed ? (
        <p id={`${field}-closed`} className="text-muted text-sm max-sm:col-span-2">
          {errors("api.CONVERSATION_CLOSED")}
        </p>
      ) : null}
      {saved ? (
        <p role="status" className="text-sm max-sm:col-span-2">
          {saved}
        </p>
      ) : null}
      {problem ? (
        <p role="alert" className="text-danger text-sm max-sm:col-span-2">
          {problem}
        </p>
      ) : null}
    </form>
  );
}
