"use client";

import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { getConversationsRetrieveQueryOptions } from "@/lib/api/client";
import type { Conversation, ConversationContact } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { ChannelAlert, NotifyToggle } from "./attention";
import { ChatPanel } from "./chat-panel";
import { ConversationList, type Filters } from "./conversation-list";
import { CustomerPanel } from "./customer-panel";
import { setUrlConversation, validId } from "./mobile";
import { useInboxRealtime } from "./use-inbox-realtime";

// En pantalla ancha lista y chat conviven; en 390 px se ve uno u otro (lista y, al abrirla, el chat).
const wide = () => typeof matchMedia === "function" && matchMedia("(min-width: 1024px)").matches;

// El Inbox (F6-03), solo lectura: la lista de conversaciones a la izquierda y el chat de la elegida
// a la derecha. El foco pasa al nombre del chat al abrir una conversación (solo cuando la lista se
// oculta) y vuelve a su fila al regresar. Sin envío, asignación, notas ni tiempo real.
// UX-INBOX-03: la abierta va en la URL (`?c=<id>`, solo elige: la API decide).
export function Inbox() {
  const t = useTranslations("inbox");
  const errors = useTranslations("errors");
  const { organization } = useTenant();
  const { slug } = organization;
  const { failed } = useReads();
  useInboxRealtime(); // los cambios llegan sin recargar (si el servicio de tiempo real está encendido)
  const [filters, setFilters] = useState<Filters>({ box: "all", status: "", unanswered: false });
  const wanted = useSearchParams()?.get("c") || null;
  // La fila elegida, solo en su organización.
  const [picked, setPicked] = useState<{ slug: string; row: Conversation } | null>(null);
  const fromList = picked?.slug === slug && picked.row.id === wanted ? picked.row : null;
  const pushed = useRef(false); // la entrada `?c=` del historial la puso esta pantalla
  const [gone, setGone] = useState<string | null>(null);
  // Un `?c=` que no viene de la lista (recargar, enlace) se lee con el detalle.
  const enabled = !!wanted && !fromList && validId(wanted);
  const deep = useQuery(
    getConversationsRetrieveQueryOptions(slug, wanted ?? "", { query: { enabled, gcTime: 0 } }),
  );
  const selected = fromList ?? (wanted && deep.data?.id === wanted ? deep.data : null);
  const notFound = !!wanted && !fromList && (!validId(wanted) || deep.isError);
  const why = notFound && (deep.isError ? failed(deep, t("denied")) : errors("api.NOT_FOUND"));
  if (why && why !== gone) setGone(why); // 401 (sin texto): la sesión lo resuelve
  useEffect(() => {
    if (!why) return;
    pushed.current = false;
    setUrlConversation(null, false); // vuelve a la lista con el texto de siempre
  }, [why]);
  // El cliente que se ve en el panel (el del detalle, no el de la fila), de la abierta; si no, el chat.
  const [shown, setCustomer] = useState<{ of: string; contact: ConversationContact } | null>(null);
  const customer = shown && shown.of === selected?.id ? shown.contact : null;
  const view = customer ? "customer" : "chat";
  const customerHeading = useRef<HTMLHeadingElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const wasCustomer = useRef(false);
  const toChat = useRef(false); // se cambió de conversación con el panel abierto: el foco va al chat nuevo
  const last = useRef<string | null>(null);
  const selectedId = selected?.id ?? null;
  useEffect(() => {
    if (selectedId) {
      last.current = selectedId;
      if (!wide()) heading.current?.focus();
    } else if (!wanted) {
      pushed.current = false;
      if (last.current) document.getElementById(`conversation-${last.current}`)?.focus();
    }
  }, [selectedId, wanted]);
  useEffect(() => {
    // Abrir el panel lleva el foco a su nombre; volver, al botón que lo abrió (el chat sigue montado).
    if (view === "customer") customerHeading.current?.focus();
    else if (wasCustomer.current && toChat.current)
      heading.current?.focus(); // otra conversación
    else if (wasCustomer.current) document.getElementById("customer-toggle")?.focus();
    if (view === "chat") toChat.current = false;
    wasCustomer.current = view === "customer";
  }, [view]);

  return (
    <section className="flex min-w-0 flex-col gap-6">
      {/* Con una conversación abierta en 390 px solo queda el título, oculto a la vista pero no a
          los lectores de pantalla: la página no se queda sin h1. */}
      <div className={cn("flex flex-col gap-2", selected && "max-lg:contents")}>
        <p className={cn("text-[13px] tracking-wide uppercase", selected && "max-lg:hidden")}>
          {t("eyebrow")}
        </p>
        <h1
          className={cn(
            "text-[28px] leading-tight font-bold tracking-[0.02em]",
            selected && "max-lg:sr-only",
          )}
        >
          {t("title")}
        </h1>
        <p className={cn("text-muted", selected && "max-lg:hidden")}>
          {t("intro", { organization: organization.name })}
        </p>
        <div className={cn(selected && "max-lg:hidden")}>
          <NotifyToggle />
        </div>
      </div>
      {gone ? (
        <p role="alert" className="text-danger">
          {gone}
        </p>
      ) : null}
      {/* En 390 px el aviso no empuja el chat fuera: se ve en la lista. */}
      <div className={cn("empty:hidden", selected && "max-lg:hidden")}>
        <ChannelAlert />
      </div>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,24rem)_minmax(0,1fr)] lg:items-start">
        <section
          aria-label={t("list.landmark")}
          className={cn("min-w-0", selected && "max-lg:hidden")}
        >
          <ConversationList
            filters={filters}
            onFilters={setFilters}
            selectedId={selectedId}
            onSelect={(next) => {
              toChat.current = view === "customer";
              setCustomer(null);
              setPicked({ slug, row: next });
              setGone(null);
              if (next.id === wanted) return;
              pushed.current ||= !wanted; // abrir añade una entrada; cambiar la reemplaza
              setUrlConversation(next.id, !wanted);
            }}
          />
        </section>
        <section aria-label={t("chat.landmark")} className="min-w-0">
          {selected ? (
            <>
              {/* El chat no se desmonta al ver al cliente: conserva su borrador y su posición. */}
              <div hidden={view === "customer"}>
                <ChatPanel
                  key={selected.id}
                  conversation={selected}
                  headingRef={heading}
                  visible={view === "chat"}
                  onBack={() => {
                    setCustomer(null);
                    // Se deshace solo la entrada propia: un enlace directo no saca del Inbox.
                    if (pushed.current) history.back();
                    else setUrlConversation(null, false);
                  }}
                  onCustomer={(contact) => setCustomer({ of: selected.id, contact })}
                />
              </div>
              {customer ? (
                <CustomerPanel
                  key={selected.id}
                  contact={customer}
                  headingRef={customerHeading}
                  onBack={() => setCustomer(null)}
                />
              ) : null}
            </>
          ) : wanted && !notFound ? (
            <p role="status" className="text-muted">
              {t("chat.loading")}
            </p>
          ) : (
            <p className="text-muted max-lg:hidden">{t("chat.choose")}</p>
          )}
        </section>
      </div>
    </section>
  );
}
