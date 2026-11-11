"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { NewTask } from "./new-task";
import type { TaskLink } from "./task-form";
import { TasksList } from "./tasks-list";

// Las tareas de un cliente o de una oportunidad (F5-08c): la pestaña de la ficha y la sección del
// detalle. Todas las del vínculo dentro del alcance de quien mira, abiertas o cerradas, con fecha
// o sin ella, y «Nueva tarea» ya vinculada.
export function LinkedTasks({ link }: { link: TaskLink }) {
  const t = useTranslations("tasks");
  const [now] = useState(() => Date.now());
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-lg font-semibold">{t("linked.title")}</h2>
      <NewTask link={link} />
      <TasksList
        params={{ ...link, limit: 200 }}
        label={t("linked.title")}
        empty={t("empty.linked")}
        links={false}
        now={now}
      />
    </section>
  );
}
