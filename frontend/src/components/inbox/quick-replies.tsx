"use client";

import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { X, Zap } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type KeyboardEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import {
  conversationQuickRepliesRender,
  getConversationQuickRepliesListQueryOptions,
} from "@/lib/api/client";
import type { QuickReplyUse, QuickReplyVariableEnum, Rendered } from "@/lib/api/model";
import { Button } from "@/components/ui/button";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { coarse, useCoarse } from "./mobile";

// `/` al inicio del cuadro y, detrás, lo que se busca sin espacios: `/deli` busca «deli». El tope
// es el de la API (`SEARCH_MAX`); con un espacio o un salto de línea ya es un mensaje, no un atajo.
const TRIGGER = /^\/(\S{0,100})$/;
const WAIT = 250; // ms sin teclear antes de buscar
const LIMIT = 50;
const SETTLE = 500; // ms tras insertar en que un Enter más no envía
const SHEET = "(max-width: 639px)"; // `max-sm`: abierta con el botón, es una hoja inferior modal
const HELP = "text-muted border-border border-t px-3 py-1.5 text-xs";
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

// Con la hoja abierta la página no se desplaza detrás (iOS no lo impide con `inert`) y al cerrar
// vuelve a su sitio. iOS no encoge el viewport con el teclado: la hoja se apoya sobre él y cabe en
// lo que queda a la vista, con el buscador arriba (`--sheet-bottom` y `--sheet-max`).
function holdPage(sheet: HTMLElement) {
  const [x, y, body, viewport] = [scrollX, scrollY, document.body.style, window.visualViewport];
  const saved = body.cssText;
  Object.assign(body, { position: "fixed", top: `${-y}px`, left: `${-x}px`, width: "100%" });
  const fit = () => {
    if (!viewport) return;
    const covered = document.documentElement.clientHeight - viewport.height - viewport.offsetTop;
    sheet.style.setProperty("--sheet-bottom", `${Math.max(0, Math.round(covered))}px`);
    sheet.style.setProperty("--sheet-max", `${Math.round(viewport.height)}px`);
  };
  const off = new AbortController();
  fit();
  viewport?.addEventListener("resize", fit, { signal: off.signal });
  viewport?.addEventListener("scroll", fit, { signal: off.signal });
  return () => {
    off.abort();
    body.cssText = saved;
    scrollTo(x, y);
  };
}

// El selector de respuestas rápidas del cuadro de respuesta (F6-08b). Escribir `/` al inicio lo
// abre; busca con `q` (con espera y cancelando la búsqueda anterior), y elegir pide `render` e
// inserta el texto resuelto en el cuadro. Nunca envía: se envía con el botón de siempre. El cuadro
// sigue siendo un `textbox` (un `textarea` no admite el rol `combobox`): lleva
// `aria-autocomplete`, `aria-controls` y `aria-activedescendant` hacia la `listbox`.
// UX-INBOX-05: el botón «Respuestas rápidas» abre la misma lista con su buscador, en un `<dialog>`
// fuera del formulario (Enter en el buscador nunca lo envía): hoja inferior modal en `max-sm`,
// desplegable en escritorio. Desde ahí, con un borrador escrito, la respuesta se añade al final.
export function useQuickReplies({
  enabled,
  conversationId,
  text,
  insert,
  box,
}: {
  enabled: boolean; // solo al responder: `render` pide `messages.send`
  conversationId: string;
  text: string;
  insert: (text: string) => void;
  box?: { current: HTMLTextAreaElement | null }; // el foco, si la hoja desaparece con él dentro
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
  // Abierta con el botón: para esta conversación y mientras se pueda responder en ella.
  const [browse, setBrowse] = useState<{ id: string; sheet: boolean } | null>(null);
  const [search, setSearch] = useState("");
  const browsing = enabled && browse?.id === conversationId;
  if (browse && !browsing) setBrowse(null);
  const slash = enabled && !browsing && typed !== undefined && !dismissed && !away;
  const open = browsing || slash;
  const query = browsing ? search : (typed ?? "");
  const button = useRef<HTMLButtonElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const shutting = useRef(false); // se cierra a propósito: el foco ya tiene adónde ir
  const tap = useCoarse();

  const q = useSettled(query, WAIT);
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
  const current = q === query && found.isSuccess && !found.isPlaceholderData;
  const groups = useMemo(() => byCategory(found.data?.results ?? []), [found.data]);
  const options = groups.flatMap((group) => group.replies);
  const active = options.find(({ id }) => id === activeId) ?? options[0];
  const optionId = (reply: QuickReplyUse) => `${list}-${reply.id}`;

  const render = useMutation<Rendered, ApiError, QuickReplyUse>({
    mutationFn: (reply) => conversationQuickRepliesRender(slug, conversationId, reply.id),
  });
  const shut = (back: boolean) => {
    shutting.current = true;
    dialog.current?.close();
    setBrowse(null);
    if (back) button.current?.focus();
  };
  const choose = (reply: QuickReplyUse | undefined) => {
    if (!reply || render.isPending) return;
    const asked = { conversationId, text };
    const append = browsing && text.trim() !== ""; // el borrador se queda; el `/atajo`, no
    render.mutate(reply, {
      onSuccess: (done) => {
        // Si se siguió escribiendo o se cambió de conversación, lo escrito manda: se descarta.
        const now = latest.current;
        if (now.conversationId !== asked.conversationId || now.text !== asked.text) return;
        const next = append ? `${asked.text}\n\n${done.text}` : done.text; // como el copiloto
        setDismissed(true); // un texto que empiece con `/` no vuelve a abrir el selector
        setMissing({ text: next, names: done.missing });
        settling.current = true;
        setTimeout(() => (settling.current = false), SETTLE);
        if (browsing) shut(false);
        insert(next); // nunca envía: el foco vuelve al cuadro, al final
      },
      onError: () => setFailedAt(text),
    });
  };

  useEffect(() => {
    if (pick.current === null || !current) return;
    const wanted = pick.current;
    pick.current = null;
    if (open && wanted === query) choose(active);
  });

  useEffect(() => {
    if (open && active)
      document.getElementById(optionId(active))?.scrollIntoView?.({ block: "nearest" });
  });

  // Una tecla del IME (Safari: sin `isComposing`, con `keyCode` 229) es suya, también Enter.
  const ime = (event: KeyboardEvent) =>
    event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
  // Devuelve si la tecla era del selector; si lo era, el cuadro no hace nada más (Enter no envía).
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!slash && !ime(event)) {
      // El Enter que eligió, sostenido o repetido al instante, no envía el texto sin revisarlo.
      const enter = event.key === "Enter" && !event.shiftKey;
      if (!enabled || !enter || coarse() || !(event.repeat || settling.current)) return false;
      event.preventDefault();
      return true;
    }
    return move(event);
  };
  const move = (event: KeyboardEvent) => {
    if (ime(event)) return false;
    const enter = event.key === "Enter" && !event.shiftKey;
    // Tab no cierra: mueve el foco y el `blur` ya la cierra; al volver con Shift+Tab, reabre.
    if (event.key === "Escape") {
      if (browsing) shut(true);
      else setDismissed(true);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const at = active ? options.indexOf(active) : -1;
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = options[(at + step + options.length) % options.length];
      if (next) setActiveId(next.id);
    } else if (enter) {
      if (current) choose(active);
      else pick.current = query; // un clic elige lo que se ve; Enter, de la lista de lo escrito
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

  const results = (
    <>
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
                  "flex min-w-0 cursor-pointer flex-col px-3 py-2 text-sm max-lg:min-h-11 max-lg:justify-center",
                  reply === active &&
                    "bg-surface-raised shadow-[inset_3px_0_0_var(--color-accent)]",
                )}
                // El foco sigue en el cuadro o el buscador: el clic (o un toque) elige sin quitárselo.
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
      <p className={cn(HELP, !browsing && "max-sm:hidden")}>{t(tap ? "keysTouch" : "keys")}</p>
    </>
  );
  const popup = slash ? (
    <div className="border-border bg-surface absolute inset-x-0 bottom-full z-20 mb-2 flex max-h-[min(20rem,45vh)] flex-col overflow-hidden rounded-[10px] border shadow-lg">
      {results}
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

  // Se muestra al montarse (modal en `max-sm`, con el buscador enfocado); al desmontarse con el
  // foco dentro (la conversación se cerró o cambió), el foco va al cuadro si aún se puede escribir.
  useEffect(() => {
    const sheet = dialog.current;
    if (!browsing || !sheet) return;
    const [modal, field] = [browse?.sheet === true, box?.current];
    shutting.current = false;
    if (!sheet.open) sheet[modal ? "showModal" : "show"]();
    sheet.querySelector("input")?.focus();
    const release = modal ? holdPage(sheet) : undefined;
    return () => {
      release?.();
      const lost = !document.activeElement || document.activeElement === document.body;
      if (!shutting.current && lost && field?.isConnected && !field.disabled) field.focus();
    };
  }, [browsing, browse?.sheet, box]);

  const sheet = browsing ? (
    <dialog
      ref={dialog}
      aria-label={t("label")}
      className={cn(
        "quick-sheet bg-surface text-foreground border-border m-0 flex-col overflow-hidden p-0 shadow-lg open:flex",
        browse?.sheet
          ? "inset-x-0 top-auto bottom-[var(--sheet-bottom,0px)] max-h-[min(85dvh,calc(var(--sheet-max,100dvh)-0.5rem))] w-full max-w-none rounded-t-[14px] border-t pb-[env(safe-area-inset-bottom)] backdrop:bg-black/40"
          : "absolute inset-x-2 bottom-full z-20 mb-2 max-h-[min(24rem,60vh)] w-auto rounded-[10px] border",
      )}
      onCancel={(event) => {
        event.preventDefault(); // Escape o «atrás» de Android: se cierra aquí, con el foco de vuelta
        shut(true);
      }}
      onClose={() => setBrowse(null)}
      onClick={(event) => event.target === event.currentTarget && shut(true)} // el fondo
      onBlur={(event) => {
        if (!browse?.sheet && !event.currentTarget.contains(event.relatedTarget)) shut(false);
      }}
    >
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 p-2">
          <input
            type="search"
            role="combobox"
            aria-label={t("search")}
            placeholder={t("search")}
            aria-expanded
            aria-autocomplete="list"
            aria-controls={list}
            aria-activedescendant={active ? optionId(active) : undefined}
            enterKeyHint="search"
            autoComplete="off"
            value={search}
            className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 min-w-0 flex-1 rounded-[10px] border px-3 text-[16px]"
            onChange={(event) => setSearch(event.target.value)}
            onKeyDown={move}
          />
          <Button
            type="button"
            variant="ghost"
            className="size-11 shrink-0 px-0"
            aria-label={t("close")}
            onClick={() => shut(true)}
          >
            <X aria-hidden />
          </Button>
        </div>
        {results}
      </div>
    </dialog>
  ) : null;
  const trigger = enabled ? (
    <Button
      ref={button}
      type="button"
      variant="ghost"
      className="border-border size-11 rounded-[14px] border px-0"
      aria-label={t("label")}
      aria-haspopup="dialog"
      aria-expanded={browsing}
      onClick={() => {
        setSearch("");
        setActiveId(null);
        setBrowse({
          id: conversationId,
          sheet: typeof matchMedia === "function" && matchMedia(SHEET).matches,
        });
      }}
    >
      <Zap aria-hidden />
    </Button>
  ) : null;

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
    ...(slash && {
      "aria-autocomplete": "list" as const,
      "aria-controls": list,
      "aria-activedescendant": active ? optionId(active) : undefined,
    }),
  };
  return { open: slash, onKey, picker, notice, field, trigger, sheet };
}
