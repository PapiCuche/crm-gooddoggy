"use client";

import { ArrowLeft } from "lucide-react";
import { useTranslations } from "next-intl";
import { type Ref, useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import type { Conversation } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { ConversationList, type Filters } from "./conversation-list";
import { Plain } from "./plain";

// En pantalla ancha lista y chat conviven; en 390 px se ve uno u otro (lista y, al abrirla, el chat).
const wide = () => typeof matchMedia === "function" && matchMedia("(min-width: 1024px)").matches;

// El Inbox (F6-03a), solo lectura: la lista de conversaciones a la izquierda y, a la derecha, la
// conversación elegida. Los mensajes llegan en F6-03b: hasta entonces el panel dice cuál se abrió
// y que la lectura viene en el siguiente corte. El foco pasa al nombre del panel al abrir una
// conversación (solo cuando la lista se oculta) y vuelve a su fila al regresar.
export function Inbox() {
  const t = useTranslations("inbox");
  const { organization } = useTenant();
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
      <div className={cn("flex flex-col gap-2", selected && "max-lg:hidden")}>
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
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
            <Opened conversation={selected} headingRef={heading} onBack={() => setSelected(null)} />
          ) : (
            <p className="text-muted max-lg:hidden">{t("chat.choose")}</p>
          )}
        </section>
      </div>
    </section>
  );
}

// El panel de la conversación abierta, sin mensajes todavía (llegan en F6-03b).
function Opened({
  conversation,
  headingRef,
  onBack,
}: {
  conversation: Conversation;
  headingRef: Ref<HTMLHeadingElement>;
  onBack: () => void;
}) {
  const t = useTranslations("inbox");
  const name = conversation.contact.display_name;
  return (
    <div className="border-border bg-surface flex flex-col gap-2 rounded-lg border p-3">
      <Button
        variant="ghost"
        className="border-border min-h-11 self-start border sm:min-h-9 lg:hidden"
        onClick={onBack}
      >
        <ArrowLeft aria-hidden />
        {t("back")}
      </Button>
      <h2
        ref={headingRef}
        tabIndex={-1}
        className="text-lg font-semibold focus-visible:outline-none"
      >
        {name === null ? t("contactUnavailable") : <Plain>{name}</Plain>}
      </h2>
      <p className="text-muted font-mono text-sm">{conversation.number}</p>
      <p className="text-muted">{t("chat.soon")}</p>
    </div>
  );
}
