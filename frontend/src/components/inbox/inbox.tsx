"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import type { Conversation, ConversationContact } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { ChatPanel } from "./chat-panel";
import { ConversationList, type Filters } from "./conversation-list";
import { CustomerPanel } from "./customer-panel";

// En pantalla ancha lista y chat conviven; en 390 px se ve uno u otro (lista y, al abrirla, el chat).
const wide = () => typeof matchMedia === "function" && matchMedia("(min-width: 1024px)").matches;

// El Inbox (F6-03), solo lectura: la lista de conversaciones a la izquierda y el chat de la elegida
// a la derecha. El foco pasa al nombre del chat al abrir una conversación (solo cuando la lista se
// oculta) y vuelve a su fila al regresar. Sin envío, asignación, notas ni tiempo real.
export function Inbox() {
  const t = useTranslations("inbox");
  const { organization } = useTenant();
  const [filters, setFilters] = useState<Filters>({ box: "all", status: "" });
  const [selected, setSelected] = useState<Conversation | null>(null);
  // El cliente que se ve en el panel (el del detalle de la conversación, no el de la fila de la lista);
  // `null`: se ve el chat.
  const [customer, setCustomer] = useState<ConversationContact | null>(null);
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
    } else if (last.current) {
      document.getElementById(`conversation-${last.current}`)?.focus();
    }
  }, [selectedId]);
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
              setSelected(next);
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
                    setSelected(null);
                  }}
                  onCustomer={setCustomer}
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
          ) : (
            <p className="text-muted max-lg:hidden">{t("chat.choose")}</p>
          )}
        </section>
      </div>
    </section>
  );
}
