"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import type { Conversation } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { ChatPanel } from "./chat-panel";
import { ConversationList, type Filters } from "./conversation-list";
import { useInboxRealtime } from "./use-inbox-realtime";

// En pantalla ancha lista y chat conviven; en 390 px se ve uno u otro (lista y, al abrirla, el chat).
const wide = () => typeof matchMedia === "function" && matchMedia("(min-width: 1024px)").matches;

// El Inbox (F6-03), solo lectura: la lista de conversaciones a la izquierda y el chat de la elegida
// a la derecha. El foco pasa al nombre del chat al abrir una conversación (solo cuando la lista se
// oculta) y vuelve a su fila al regresar. Sin envío, asignación, notas ni tiempo real.
export function Inbox() {
  const t = useTranslations("inbox");
  const { organization } = useTenant();
  useInboxRealtime(); // los cambios llegan sin recargar (si el servicio de tiempo real está encendido)
  const [filters, setFilters] = useState<Filters>({ box: "all", status: "" });
  const [selected, setSelected] = useState<Conversation | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
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
            onSelect={setSelected}
          />
        </section>
        <section aria-label={t("chat.landmark")} className="min-w-0">
          {selected ? (
            <ChatPanel
              key={selected.id}
              conversation={selected}
              headingRef={heading}
              onBack={() => setSelected(null)}
            />
          ) : (
            <p className="text-muted max-lg:hidden">{t("chat.choose")}</p>
          )}
        </section>
      </div>
    </section>
  );
}
