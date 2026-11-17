"use client";

import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import {
  conversationQuickRepliesRender,
  getConversationQuickRepliesListQueryOptions,
} from "@/lib/api/client";
import type { QuickReplyUse, QuickReplyVariableEnum, Rendered } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

// `/` al inicio del cuadro y, detrás, lo que se busca sin espacios: `/deli` busca «deli». El tope
// es el de la API (`SEARCH_MAX`); con un espacio o un salto de línea ya es un mensaje, no un atajo.
const TRIGGER = /^\/(\S{0,100})$/;
const WAIT = 250; // ms sin teclear antes de buscar
const LIMIT = 50;
const SETTLE = 500; // ms tras insertar en que un Enter más no envía
const VARIABLE = {
  "contact.first_name": "firstName",
  "contact.name": "name",
  "org.name": "org",
} as const satisfies Record<QuickReplyVariableEnum, string>;

function useSettled<T>(value: T, ms: number) {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

type Group = { id: string; name: string; order: number; replies: QuickReplyUse[] };

// Agrupadas por categoría (su orden y luego su nombre); dentro, en el orden de la API.
export function byCategory(replies: readonly QuickReplyUse[]): Group[] {
  const groups = new Map<string, Group>();
  for (const reply of replies) {
    const { id, name, sort_order: order } = reply.category;
    const group = groups.get(id) ?? { id, name, order, replies: [] };
    group.replies.push(reply);
    groups.set(id, group);
  }
  return [...groups.values()].sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

// El selector de respuestas rápidas del cuadro de respuesta (F6-08b). Escribir `/` al inicio lo
// abre; busca con `q` (con espera y cancelando la búsqueda anterior), y elegir pide `render` e
// inserta el texto resuelto en el cuadro. Nunca envía: se envía con el botón de siempre. El cuadro
// sigue siendo un `textbox` (un `textarea` no admite el rol `combobox`): lleva
// `aria-autocomplete`, `aria-controls` y `aria-activedescendant` hacia la `listbox`.
export function useQuickReplies({
  enabled,
  conversationId,
  text,
  insert,
}: {
  enabled: boolean; // solo al responder: `render` pide `messages.send`
  conversationId: string;
  text: string;
  insert: (text: string) => void;
}) {
  const t = useTranslations("inbox.quickReplies");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization } = useTenant();
  const slug = organization.slug;
  const list = useId();
  const typed = TRIGGER.exec(text)?.[1];
  const [dismissed, setDismissed] = useState(false); // Escape: cerrado hasta quitar la `/`
  // Lo que faltó en el último texto insertado; el aviso se va en cuanto el texto cambia.
  const [missing, setMissing] = useState<{ text: string; names: QuickReplyVariableEnum[] }>();
  const [away, setAway] = useState(false); // el foco salió del cuadro
  const [activeId, setActiveId] = useState<string | null>(null);
  const [failedAt, setFailedAt] = useState<string | null>(null); // el texto cuando falló `render`
  const pick = useRef<string | null>(null); // Enter antes de que llegue la búsqueda
  // Lo último que se ve: un `render` que vuelve tarde solo inserta si nada cambió desde que se pidió.
  const latest = useRef({ conversationId, text });
  const settling = useRef(false); // recién insertado: un Enter más todavía no envía
  useEffect(() => {
    latest.current = { conversationId, text };
  });
  if (typed === undefined && dismissed) setDismissed(false);
  const open = enabled && typed !== undefined && !dismissed && !away;

  const q = useSettled(typed ?? "", WAIT);
  const found = useQuery({
    ...getConversationQuickRepliesListQueryOptions(
      slug,
      conversationId,
      q ? { q, limit: LIMIT } : { limit: LIMIT },
    ),
    enabled: open,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  });
  // Mientras la búsqueda no alcanza lo escrito, la lista es la anterior: Enter no elige de ella,
  // espera a la buena y elige su primera opción (si se sigue escribiendo, se olvida).
  const current = q === (typed ?? "") && found.isSuccess && !found.isPlaceholderData;
  const groups = useMemo(() => byCategory(found.data?.results ?? []), [found.data]);
  const options = groups.flatMap((group) => group.replies);
  const active = options.find(({ id }) => id === activeId) ?? options[0];
  const optionId = (reply: QuickReplyUse) => `${list}-${reply.id}`;

  const render = useMutation<Rendered, ApiError, QuickReplyUse>({
    mutationFn: (reply) => conversationQuickRepliesRender(slug, conversationId, reply.id),
  });
  const choose = (reply: QuickReplyUse | undefined) => {
    if (!reply || render.isPending) return;
    const asked = { conversationId, text };
    render.mutate(reply, {
      onSuccess: (done) => {
        // Si se siguió escribiendo o se cambió de conversación, lo escrito manda: se descarta.
        const now = latest.current;
        if (now.conversationId !== asked.conversationId || now.text !== asked.text) return;
        setDismissed(true); // un texto que empiece con `/` no vuelve a abrir el selector
        setMissing({ text: done.text, names: done.missing });
        settling.current = true;
        setTimeout(() => (settling.current = false), SETTLE);
        insert(done.text);
      },
      onError: () => setFailedAt(text),
    });
  };

  useEffect(() => {
    if (pick.current === null || !current) return;
    const wanted = pick.current;
    pick.current = null;
    if (open && wanted === text) choose(active);
  });

  useEffect(() => {
    if (open && active)
      document.getElementById(optionId(active))?.scrollIntoView?.({ block: "nearest" });
  });

  // Devuelve si la tecla era del selector; si lo era, el cuadro no hace nada más (Enter no envía).
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Una tecla del IME (Safari: sin `isComposing`, con `keyCode` 229) es suya, también Enter.
    const composing = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
    if (composing) return false;
    const enter = event.key === "Enter" && !event.shiftKey;
    if (!open) {
      // El Enter que eligió, sostenido o repetido al instante, no envía el texto sin revisarlo.
      if (!enabled || !enter || !(event.repeat || settling.current)) return false;
      event.preventDefault();
      return true;
    }
    // Tab no cierra: mueve el foco y el `blur` ya la cierra; al volver con Shift+Tab, reabre.
    if (event.key === "Escape") {
      setDismissed(true);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const at = active ? options.indexOf(active) : -1;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = options[(at + step + options.length) % options.length];
      if (next) setActiveId(next.id);
    } else if (enter) {
      if (current) choose(active);
      else pick.current = text; // un clic elige lo que se ve; Enter, de la lista de lo escrito
    } else {
      return false;
    }
    event.preventDefault();
    event.stopPropagation();
    return true;
  };

  const failure =
    render.error && failedAt === text
      ? render.error.status === 404
        ? t("gone")
        : errors(`api.${apiErrorKey(render.error)}`)
      : found.isError
        ? errors(`api.${apiErrorKey(found.error)}`)
        : null;
  const state = render.isPending
    ? t("inserting")
    : found.isPending
      ? t("loading")
      : options.length === 0 && !found.isError
        ? t("empty")
        : null;

  const popup = open ? (
    <div className="border-border bg-surface absolute inset-x-0 bottom-full z-20 mb-2 flex max-h-[min(20rem,45vh)] flex-col overflow-hidden rounded-[10px] border shadow-lg">
      <div
        role="listbox"
        id={list}
        aria-label={t("label")}
        aria-busy={!current}
        className="min-h-0 overflow-y-auto overscroll-contain py-1"
      >
        {groups.map((group) => (
          <div key={group.id} role="group" aria-labelledby={`${list}-g-${group.id}`}>
            <p
              role="presentation"
              id={`${list}-g-${group.id}`}
              className="text-muted px-3 pt-2 pb-1 text-xs font-semibold"
            >
              {group.name}
            </p>
            {group.replies.map((reply) => (
              <div
                key={reply.id}
                id={optionId(reply)}
                role="option"
                aria-selected={reply === active}
                className={cn(
                  "flex min-w-0 cursor-pointer flex-col px-3 py-2 text-sm",
                  reply === active &&
                    "bg-surface-raised shadow-[inset_3px_0_0_var(--color-accent)]",
                )}
                // El foco sigue en el cuadro: el clic elige sin quitárselo.
                onMouseDown={(event) => event.preventDefault()}
                onMouseMove={() => setActiveId(reply.id)}
                onClick={() => choose(reply)}
              >
                <span className="flex min-w-0 items-baseline gap-2">
                  <span className="max-w-[60%] min-w-0 truncate font-mono font-medium">
                    /{reply.shortcut}
                  </span>
                  <span className="min-w-0 truncate">
                    <bdi>{reply.title}</bdi>
                  </span>
                </span>
                <span className="text-muted truncate text-xs">
                  <bdi>{reply.body}</bdi>
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
      {state || failure ? (
        <p
          className={cn("border-border border-t px-3 py-2 text-sm", failure && "text-danger")}
          role={failure ? "alert" : undefined}
        >
          {failure ?? state}
        </p>
      ) : null}
      <p className="text-muted border-border border-t px-3 py-1.5 text-xs max-sm:hidden">
        {t("keys")}
      </p>
    </div>
  ) : null;
  // La región `status` está siempre montada: una que aparece con su texto no siempre se anuncia.
  const picker = (
    <>
      <p role="status" className="sr-only">
        {open && current ? t("count", { count: options.length }) : ""}
      </p>
      {popup}
    </>
  );

  const notice =
    missing && missing.text === text && missing.names.length > 0 ? (
      <p role="status" className="text-sm max-sm:col-span-2">
        {t("missing", {
          fields: format.list(
            missing.names.map((name) => t(`variable.${VARIABLE[name]}`)),
            { type: "conjunction" },
          ),
        })}
      </p>
    ) : null;

  const field = {
    onFocus: () => setAway(false),
    onBlur: () => setAway(true),
    ...(open && {
      "aria-autocomplete": "list" as const,
      "aria-controls": list,
      "aria-activedescendant": active ? optionId(active) : undefined,
    }),
  };
  return { open, onKey, picker, notice, field };
}
