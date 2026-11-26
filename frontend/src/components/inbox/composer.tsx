"use client";

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { FileText, ImageIcon, Paperclip, RotateCw, Send, StickyNote, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import {
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  useEffect,
  useId,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  conversationMediaSend,
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
import { type ApiErrorKey, apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { useDraftsValue } from "./drafts";
import { fileSize } from "./media-file";
import { coarse, useCoarse } from "./mobile";
import { useQuickReplies } from "./quick-replies";

// El tope de la API (`BODY_MAX`, 4096 caracteres; se cuentan puntos de código, como ella).
export const BODY_MAX = 4096;
// El pie de foto de un medio (F7-09b2): hasta 1024 unidades UTF-16, como `String.length`.
export const CAPTION_MAX = 1024;
// Lo que acepta la ruta de medios y el tope de cada tipo (`outbound_media.CAPS`). Es comodidad: la
// ruta decide el tipo real por el contenido y vuelve a medir; aquí solo se evita subir en vano.
const FILES: Record<string, { kind: "IMAGE" | "DOCUMENT"; cap: number }> = {
  "image/jpeg": { kind: "IMAGE", cap: 5 << 20 },
  "image/png": { kind: "IMAGE", cap: 5 << 20 },
  "application/pdf": { kind: "DOCUMENT", cap: 10 << 20 },
};
// Un 411, 413 o 415 sin el `code` de la API (lo respondió el proxy, p. ej. su tope del cuerpo).
const BY_STATUS: Record<number, ApiErrorKey> = {
  411: "LENGTH_REQUIRED",
  413: "MEDIA_TOO_LARGE",
  415: "MEDIA_TYPE_NOT_ALLOWED",
};
const failure = (error: ApiError): ApiErrorKey => {
  const key = apiErrorKey(error);
  return (key === "INTERNAL_ERROR" && BY_STATUS[error.status]) || key;
};
// Reenviar el mismo archivo daría lo mismo: se quita y se explica, sin «Reintentar». Sin veredicto
// del antivirus (503 `MEDIA_SCAN_UNAVAILABLE`) no se guardó nada: ese sí se reintenta. Un archivo que
// pasa los topes del antivirus (422 `MEDIA_TOO_COMPLEX`) no se presenta como virus.
const DROP_FILE_ON: ApiErrorKey[] = [
  "MEDIA_TOO_LARGE",
  "MEDIA_TYPE_NOT_ALLOWED",
  "MEDIA_INFECTED",
  "MEDIA_TOO_COMPLEX",
  "MEDIA_NOT_SUPPORTED_BY_CHANNEL",
];
// `draft`: la clave del borrador al enviar; los callbacks corren aunque el cuadro ya no esté montado.
type Outgoing = SendMessageRequest & { file?: File; draft: string };
// Un 409 de la política de envío: lo que cambió puede estar también en la conversación.
const REFRESH_ON = [
  "CONVERSATION_CLOSED",
  "CHANNEL_UNAVAILABLE",
  "CONTACT_BLOCKED",
  "WINDOW_CLOSED",
  "CONSENT_REVOKED", // fuera de la ventana, la etiqueta Human Agent y una baja (F7b-05d)
];
type Pages = InfiniteData<PaginatedMessageList, string>;

export const messagesKey = (slug: string, id: string) =>
  [...getConversationMessagesListQueryKey(slug, id), "pages"] as const;

export type Mode = "reply" | "note";
// Pone un texto en el cuadro de respuesta (el del copiloto, F8-09a): nunca envía.
export type Inserter = (text: string) => void;
const SETTLE = 500; // ms tras insertar en que un Enter no envía (el que pulsó «Insertar», repetido)
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
  inserter,
  tools,
}: {
  conversation: Conversation;
  modes: { reply: boolean; note: boolean };
  onQueued: () => void;
  inserter?: RefObject<Inserter | null>; // el copiloto inserta en «Responder», que pasa a verse
  tools?: ReactNode; // al final de la fila de los modos («Sugerir respuesta», F8-09a)
}) {
  const t = useTranslations("inbox.composer");
  const [chosen, setChosen] = useState<Mode>(modes.reply ? "reply" : "note");
  const replyInserter = useRef<Inserter | null>(null);
  useImperativeHandle(inserter, () => (text) => {
    setChosen("reply");
    replyInserter.current?.(text);
  });
  const shown: Mode = modes[chosen] ? chosen : modes.reply ? "reply" : "note";
  const both = modes.reply && modes.note;
  return (
    <div className="border-border relative flex min-w-0 shrink-0 flex-col gap-2 border-t p-2 lg:p-3">
      {both || tools ? (
        <div className="flex min-w-0 gap-2">
          {both ? (
            <div
              role="group"
              aria-label={t("mode.label")}
              className="flex min-w-0 gap-2 max-sm:flex-1"
            >
              {(["reply", "note"] as const).map((mode) => (
                <Button
                  key={mode}
                  type="button"
                  variant={shown === mode ? "accent" : "ghost"}
                  className="border-border h-9 min-w-0 border max-lg:min-h-11 max-sm:flex-1"
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
          {tools ? <div className="ml-auto flex gap-2">{tools}</div> : null}
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
            inserter={mode === "reply" ? replyInserter : undefined}
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
  inserter,
}: {
  mode: Mode;
  active: boolean;
  tabbed: boolean; // hay conmutador: su botón ya dice el modo, la etiqueta queda para lectores
  conversation: Conversation;
  onQueued: () => void;
  inserter?: RefObject<Inserter | null>;
}) {
  const t = useTranslations("inbox.composer");
  const errors = useTranslations("errors");
  const { organization, user } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const field = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const drafts = useDraftsValue((all) => all); // el texto es el borrador: se edita por su clave
  const draft = drafts.key(conversation.id, mode);
  const text = useDraftsValue((all) => all.get(draft).text);
  const busy = useDraftsValue((all) => all.get(draft).sending);
  const setText = (next: string | ((current: string) => string)) =>
    drafts.update(draft, ({ text }) => ({ text: typeof next === "string" ? next : next(text) }));
  const forget = (sent: Outgoing) =>
    drafts.update(sent.draft, ({ attempt }) =>
      attempt?.id === sent.client_message_id ? { attempt: null } : {},
    );
  const sentFile = useRef<File | null>(null); // el archivo del último intento de este cuadro
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null); // el archivo elegido no se adjuntó
  const picker = useRef<HTMLInputElement>(null);
  const settleUntil = useRef(0); // hasta cuándo un Enter no envía lo recién insertado
  const tap = useCoarse() ? "Touch" : ""; // la ayuda de teclas y la tecla del teléfono
  const [placed, setPlaced] = useState(0); // cada inserción del copiloto: enfocar al final
  const format = useFormatter();
  const labels = useTranslations("inbox.media");
  const note = mode === "note";
  const key = messagesKey(slug, conversation.id);
  // Con un archivo, el texto es su pie de foto: otro tope, contado en unidades UTF-16. Una respuesta
  // sigue el tope de su canal (F7b-16, `channel.text_max`): Instagram cuenta bytes UTF-8.
  // Sin los campos (una API anterior, durante un despliegue o un rollback) vale lo de antes.
  const { text_max, text_unit, accepts_media } = conversation.channel;
  const media = accepts_media !== false;
  const bytes = !note && !file && text_unit === "BYTES";
  const max = file ? CAPTION_MAX : note ? BODY_MAX : (text_max ?? BODY_MAX);
  // El navegador manda cada salto de línea del formulario como `\r\n`: cuenta dos.
  const count = file
    ? text.length + (text.match(/\n/g)?.length ?? 0)
    : bytes
      ? new TextEncoder().encode(text).length
      : Array.from(text).length;
  // Una nota se admite también en una conversación cerrada; una respuesta no.
  const closed = !note && conversation.status === "CLOSED";
  const over = count > max;
  const empty = text.trim() === "" && !file;
  const kind = (chosen: File) => FILES[chosen.type]?.kind ?? "IMAGE"; // solo se adjunta uno de `FILES`
  // `/atajo` o su botón abren las respuestas rápidas (F6-08b): elegir solo cambia el cuadro.
  const quick = useQuickReplies({
    enabled: !note && !closed,
    conversationId: conversation.id,
    box,
    text,
    insert: (value) => {
      settleUntil.current = performance.now() + SETTLE; // un segundo toque en «Enviar» no lo envía
      setText(value);
      setProblem(null);
      box.current?.focus();
      requestAnimationFrame(() => box.current?.setSelectionRange(value.length, value.length));
    },
  });

  // El texto del copiloto se suma a lo ya escrito (no lo borra) y queda editable; no se envía. Si
  // llega mientras sale un envío, ese envío solo quita del cuadro lo que él mandó (`onSuccess`).
  useImperativeHandle(inserter, () => (value) => {
    settleUntil.current = performance.now() + SETTLE; // otra inserción alarga la espera, no la acorta
    setText((current) => (current.trim() ? `${current}\n\n${value}` : value));
    setProblem(null);
    setRefusal(null);
    setSaved(null);
    setPlaced((n) => n + 1);
  });
  useEffect(() => {
    const field = box.current;
    if (!placed || !field) return;
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  }, [placed]);

  const send = useMutation<Message, ApiError, Outgoing>({
    mutationFn: ({ file: attached, client_message_id, body }) => {
      const request = { client_message_id, body }; // la clave del borrador no va a la API
      // Sin respuesta en el plazo es un error de red: libera el cuadro; el reintento lleva el id.
      const options = { signal: AbortSignal.timeout(attached ? 120_000 : 30_000) };
      return note
        ? conversationNotesCreate(slug, conversation.id, request, options)
        : attached
          ? conversationMediaSend(
              slug,
              conversation.id,
              {
                client_message_id: request.client_message_id,
                file: attached,
                // Sin pie de foto el campo no va: la ruta responde 400 a un `caption` vacío.
                caption: request.body.trim() ? request.body : undefined,
              },
              options,
            )
          : conversationMessagesSend(slug, conversation.id, request, options);
    },
    onSettled: (_done, _error, request) => drafts.update(request.draft, () => ({ sending: false })),
    onMutate: async (request) => {
      await client.cancelQueries({ queryKey: key });
      onQueued();
      const media = request.file ? kind(request.file) : null;
      const pending: Message = {
        ...{ id: `pending-${request.client_message_id}`, sender_type: "USER" },
        ...{ direction: note ? "INTERNAL" : "OUTBOUND" },
        ...{ message_type: note ? "NOTE" : (media ?? "TEXT") },
        ...{ sender_user_id: user.id },
        ...{ body: media && !request.body.trim() ? labels(media) : request.body },
        ...{ client_message_id: request.client_message_id },
        ...{ delivery_status: note ? null : "QUEUED", created_at: new Date().toISOString() },
        ...{ sent_at: null, delivered_at: null, read_at: null, failed_at: null },
        ...{ provider_error_code: null, provider_error_detail: null, attachment: null },
        dispatch_attempts: 0,
      };
      client.setQueryData<Pages>(key, (data) =>
        edit(data, (rows, newest) => (newest ? [pending, ...rows] : rows)),
      );
      setProblem(null);
      setRefusal(null);
      setSaved(null);
    },
    onSuccess: (done, request) => {
      forget(request); // un id por acción: el siguiente texto lleva otro
      // Quita solo lo enviado: lo insertado mientras salía se queda y no se vuelve a enviar.
      drafts.update(request.draft, ({ text: current }) => ({
        text: current.startsWith(request.body)
          ? current.slice(request.body.length).replace(/^\n\n/, "")
          : current,
      }));
      setFile((current) => (current === request.file ? null : current)); // solo el que salió
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
      // `onMutate` canceló la lectura en curso: si era la primera, el chat se quedaría «Cargando…».
      void client.invalidateQueries({ queryKey: key });
      // Ese id ya es de otro envío: reintentar con él chocaría siempre. Si el texto creció tras un
      // fallo, lo primero pudo haber salido: se avisa en lugar de mandarlo otra vez.
      const tried = drafts.get(request.draft).attempt;
      const grown = tried?.id === request.client_message_id && tried.first !== request.body;
      if (error.code === "CLIENT_MESSAGE_ID_TAKEN") forget(request);
      const reason = failure(error);
      if (DROP_FILE_ON.includes(reason)) {
        forget(request);
        setFile(null);
        setRefusal(errors(`api.${reason}`));
        return;
      }
      if (REFRESH_ON.includes(error.code)) {
        void client.invalidateQueries({
          queryKey: getConversationsRetrieveQueryKey(slug, conversation.id),
        });
      }
      setProblem(
        error.status === 403 || error.status === 404
          ? t(note ? "deniedNote" : "denied")
          : reason === "CLIENT_MESSAGE_ID_TAKEN" && request.file
            ? t("idTakenMedia")
            : reason === "CLIENT_MESSAGE_ID_TAKEN" && grown
              ? t("idTakenGrown")
              : errors(`api.${reason}`),
      );
    },
  });

  const submit = () => {
    if (drafts.get(draft).sending || closed || over || empty) return;
    if (performance.now() < settleUntil.current) return; // recién insertado y sin tocar: se revisa
    // Reintentar reutiliza el id mientras el texto empiece por el que falló (también con lo que se
    // insertó o escribió después) y el archivo sea el mismo: si el primero llegó al servidor sin
    // respuesta, el mismo id no deja que salga dos veces. Otro texto empieza un intento nuevo.
    let tried = drafts.get(draft).attempt;
    const sameFile = tried?.media ? file !== null && file === sentFile.current : file === null;
    if (!tried || !text.startsWith(tried.first) || !sameFile) {
      tried = { id: uuid(), first: text, media: file !== null };
    }
    sentFile.current = file;
    drafts.update(draft, () => ({ attempt: tried, sending: true }));
    send.mutate({ client_message_id: tried.id, body: text, file: file ?? undefined, draft });
  };
  // Solo JPEG, PNG o PDF dentro de su tope; si no, se explica y el archivo no queda adjunto.
  const pick = (chosen: File | undefined) => {
    if (!chosen) return;
    const accepted = Object.hasOwn(FILES, chosen.type) ? FILES[chosen.type] : null;
    const refused = !accepted
      ? "MEDIA_TYPE_NOT_ALLOWED"
      : chosen.size > accepted.cap
        ? "MEDIA_TOO_LARGE"
        : null;
    setRefusal(refused && errors(`api.${refused}`));
    if (!refused) setFile(chosen);
    box.current?.focus();
  };
  const keys = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (quick.onKey(event)) return; // con el selector abierto, Enter elige y no envía
    // Enter envía; Mayús+Enter agrega una línea. Durante una composición (IME) Enter es del IME;
    // Safari lo entrega ya sin `isComposing`, pero con `keyCode` 229.
    const composing = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
    if (event.key !== "Enter" || event.shiftKey || composing || coarse()) return;
    event.preventDefault();
    submit();
  };
  const idle = note ? t("save") : t("send");
  // En menos de 640 px el formulario es una cuadrícula de dos columnas: la respuesta lleva el cuadro
  // y un botón de icono en la misma fila; la nota, el cuadro y debajo su aviso con «Guardar nota».
  const first = tabbed ? "max-sm:row-start-1" : "max-sm:row-start-2";
  const second = tabbed ? "max-sm:row-start-2" : "max-sm:row-start-3";

  const action = (
    <Button
      type="submit"
      variant="primary"
      size="lg"
      className={cn(
        "max-lg:h-11 max-sm:col-start-2 max-sm:self-center",
        note ? second : cn("max-sm:size-11 max-sm:px-0", first),
      )}
      disabled={closed || empty || over}
      aria-disabled={busy}
    >
      {note ? (
        <StickyNote aria-hidden />
      ) : problem ? (
        <RotateCw aria-hidden />
      ) : (
        <Send aria-hidden />
      )}
      <span className={note ? undefined : "max-sm:sr-only"}>
        {busy ? t(note ? "saving" : "sending") : problem ? t("retry") : idle}
      </span>
    </Button>
  );

  const form = (
    <form
      hidden={!active}
      className={cn(
        "relative flex min-w-0 flex-col gap-2 max-sm:grid max-sm:grid-cols-[minmax(0,1fr)_auto] max-sm:gap-x-2 max-sm:gap-y-1",
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
          "text-sm font-medium max-sm:col-span-2 max-sm:row-start-1 max-sm:group-data-composing:sr-only",
          tabbed && "max-lg:sr-only",
        )}
      >
        {t(file ? "label.caption" : `label.${mode}`)}
      </label>
      {quick.picker}
      {/* 16 px: por debajo, Safari en iOS amplía la página al enfocar. */}
      <textarea
        id={field}
        ref={box}
        rows={1}
        value={text}
        disabled={closed}
        readOnly={busy}
        placeholder={note || closed ? undefined : t("placeholder")}
        enterKeyHint={tap ? "enter" : "send"}
        {...quick.field}
        aria-describedby={`${field}-count ${field}-keys${note ? ` ${field}-hint` : ""}${file ? ` ${field}-file` : ""}${closed ? ` ${field}-closed` : ""}`}
        aria-invalid={over || problem ? true : undefined}
        className={cn(
          "bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger field-sizing-content max-h-24 min-h-11 w-full min-w-0 resize-none rounded-[10px] border px-3 py-2 text-[16px] wrap-anywhere disabled:opacity-60 lg:field-sizing-fixed lg:max-h-40 lg:min-h-16 lg:p-3",
          note && "border-dashed",
          note ? "max-sm:col-span-2" : "max-sm:col-start-1",
          first,
        )}
        onChange={(event) => {
          setText(event.target.value);
          setProblem(null);
          setRefusal(null);
          setSaved(null);
        }}
        onKeyDown={keys}
      />
      {file ? (
        <div className="border-border bg-surface-raised flex min-w-0 items-center gap-2 rounded-[10px] border py-1 pr-1 pl-3 text-sm max-sm:col-span-2">
          {kind(file) === "DOCUMENT" ? (
            <FileText aria-hidden className="size-4 shrink-0" />
          ) : (
            <ImageIcon aria-hidden className="size-4 shrink-0" />
          )}
          <p id={`${field}-file`} className="flex min-w-0 flex-1 flex-col">
            <span className="sr-only">
              {t("attached", { name: file.name, detail: fileSize(format, file.size) })}
            </span>
            <span aria-hidden className="truncate font-medium">
              <bdi>{file.name}</bdi>
            </span>
            <span aria-hidden className="text-muted text-xs">
              {fileSize(format, file.size)} · {t("captionHint")}
            </span>
          </p>
          <Button
            type="button"
            variant="ghost"
            className="size-9 shrink-0 px-0 max-lg:size-11"
            aria-label={t("remove")}
            disabled={busy}
            onClick={() => {
              setFile(null);
              setProblem(null);
              setRefusal(null);
              box.current?.focus();
            }}
          >
            <X aria-hidden />
          </Button>
        </div>
      ) : null}
      <p role="status" className="sr-only">
        {busy ? t(note ? "saving" : "sending") : ""}
      </p>
      {/* El tope se anuncia solo al cruzar el 90 % y el 100 %: el texto depende del nivel, no de la cuenta. */}
      <p aria-live="polite" className="sr-only">
        {over
          ? t(note ? "overLimit.note" : bytes ? "overLimit.replyBytes" : "overLimit.reply")
          : count >= max * 0.9
            ? t(bytes ? "nearLimitBytes" : "nearLimit")
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
          {/* El aviso de la nota se ve siempre; en 390 px la ayuda de teclas queda para lectores. */}
          {note ? (
            <span id={`${field}-hint`} className="text-foreground font-medium">
              {t("hint.note")}
            </span>
          ) : null}
          <span id={`${field}-keys`} className="max-lg:sr-only">
            {t(note ? `hint.noteKeys${tap}` : quick.open ? "hint.pick" : `hint.reply${tap}`)}
          </span>
          <span
            id={`${field}-count`}
            className={cn(
              over && "text-danger font-medium",
              !over && count < max * 0.9 && "max-lg:sr-only",
            )}
          >
            {bytes
              ? t(over ? "overBytes" : "countBytes", { count, max, over: count - max })
              : over
                ? t("over", { over: count - max })
                : t("count", { count, max })}
          </span>
        </div>
        {note ? (
          action
        ) : (
          // La respuesta lleva el clip junto a «Enviar»; en 390 px, los dos en la fila del cuadro.
          <div
            className={cn("flex items-center gap-2 max-sm:col-start-2 max-sm:self-center", first)}
          >
            {/* Messenger e Instagram aún no envían adjuntos (`channel.accepts_media`): sin clip. */}
            <input
              ref={picker}
              type="file"
              hidden
              accept="image/jpeg,image/png,application/pdf,.jpg,.jpeg,.png,.pdf"
              onChange={(event) => {
                pick(event.target.files?.[0]);
                event.target.value = ""; // el mismo archivo otra vez también avisa
              }}
            />
            {quick.trigger}
            {media ? (
              <Button
                type="button"
                variant="ghost"
                className="border-border size-11 rounded-[14px] border px-0 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
                aria-label={t("attach")}
                title={t("attach")}
                disabled={closed}
                aria-disabled={busy}
                onClick={() => busy || picker.current?.click()}
              >
                <Paperclip aria-hidden />
              </Button>
            ) : null}
            {action}
          </div>
        )}
      </div>
      {closed ? (
        <p id={`${field}-closed`} className="text-muted text-sm max-sm:col-span-2">
          {errors("api.CONVERSATION_CLOSED")}
        </p>
      ) : null}
      {quick.notice}
      {saved ? (
        <p role="status" className="text-sm max-sm:col-span-2">
          {saved}
        </p>
      ) : null}
      {problem || refusal ? (
        <p role="alert" className="text-danger text-sm max-sm:col-span-2">
          {problem ?? refusal}
        </p>
      ) : null}
    </form>
  );
  // La hoja va fuera del formulario: Enter en su buscador no puede enviarlo.
  return (
    <>
      {quick.sheet}
      {form}
    </>
  );
}
