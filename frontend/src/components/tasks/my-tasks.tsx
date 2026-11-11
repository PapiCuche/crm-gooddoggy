"use client";

import { useTimeZone, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import type { TasksListParams } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { dayRange } from "./task-time";
import { TasksList } from "./tasks-list";

const TABS = ["today", "overdue", "upcoming"] as const;

// «Mis tareas» (F5-08c, E10-01): las tareas de quien mira, en Hoy, Vencidas y Próximas. Los
// límites del día salen de la zona de la pantalla y los pide el cliente (el servidor no los
// calcula, F5-08b); la lista de vencidas descarta las de todo el día de hoy. Una tarea sin fecha no sale en ninguna.
export function MyTasks() {
  const t = useTranslations("tasks");
  const zone = useTimeZone() ?? "America/Lima";
  const { organization } = useTenant();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    // Una pestaña que se deja abierta pasa de día: al volver a ella, «hoy» y lo vencido se
    // calculan con la hora de ahora (los límites del día cambian y las listas se vuelven a pedir).
    const refresh = () => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);
  const [tab, setTab] = useState<(typeof TABS)[number]>("today");
  const buttons = useRef<Partial<Record<string, HTMLButtonElement | null>>>({});
  const { start, next } = dayRange(now, zone);
  const filters: Record<(typeof TABS)[number], TasksListParams> = {
    today: {
      mine: true,
      due_from: new Date(start).toISOString(),
      due_to: new Date(next).toISOString(),
    },
    overdue: { mine: true, overdue: true },
    upcoming: { mine: true, due_from: new Date(next).toISOString() },
  };

  return (
    <section className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      <div
        role="tablist"
        aria-label={t("tabs.label")}
        className="flex flex-wrap gap-2"
        // Las flechas, Inicio y Fin mueven entre pestañas y las abren (APG, activación automática).
        onKeyDown={(event) => {
          const at = TABS.indexOf(tab);
          const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: -1 }[event.key];
          if (to === undefined) return;
          event.preventDefault();
          const key = TABS.at(to % TABS.length)!;
          setTab(key);
          buttons.current[key]?.focus();
        }}
      >
        {TABS.map((key) => (
          <button
            key={key}
            ref={(node) => {
              buttons.current[key] = node;
            }}
            type="button"
            role="tab"
            id={`tasks-${key}-tab`}
            aria-selected={tab === key}
            aria-controls="tasks-panel"
            tabIndex={tab === key ? 0 : -1}
            onClick={() => setTab(key)}
            className={cn(
              "flex h-11 items-center rounded-lg border px-4 text-sm lg:h-9",
              tab === key
                ? "border-foreground/10 bg-accent text-accent-foreground"
                : "border-border text-muted hover:text-foreground",
            )}
          >
            {t(`tabs.${key}`)}
          </button>
        ))}
      </div>
      <div
        role="tabpanel"
        id="tasks-panel"
        tabIndex={0}
        aria-labelledby={`tasks-${tab}-tab`}
        className="flex flex-col gap-3"
      >
        <TasksList
          key={tab}
          params={{ ...filters[tab], limit: 100 }}
          label={t(`tabs.${tab}`)}
          empty={t(`empty.${tab}`)}
          links
          keep={tab === "overdue" ? "overdue" : undefined}
          now={now}
        />
      </div>
    </section>
  );
}
