"use client";

import { useQuery } from "@tanstack/react-query";
import { useTimeZone, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { getTasksListQueryKey, tasksList } from "@/lib/api/client";

import { dayRange, isOverdue } from "./task-time";

// El contador de vencidas de la entrada «Mis tareas» del menú (F5-08c): las mías, abiertas y con
// el día pasado, hasta 100 (si hay más, «100+»). Es comodidad: si la lectura falla o no hay
// ninguna, no se pinta nada. Cuelga de la clave de las listas de tareas: crear o cambiar una
// tarea lo vuelve a pedir.
export function OverdueCount() {
  const t = useTranslations("tasks");
  const zone = useTimeZone() ?? "America/Lima";
  const slug = useTenant().organization.slug;
  const params = { mine: true, overdue: true, limit: 100 };
  const count = useQuery({
    queryKey: [...getTasksListQueryKey(slug, params), "overdue-count"],
    queryFn: async ({ signal }) => {
      const page = await tasksList(slug, params, { signal });
      const { start } = dayRange(Date.now(), zone);
      const late = page.results.filter((task) => isOverdue(task, start, Date.now())).length;
      return { late, more: page.next !== null };
    },
  });
  if (!count.data || count.data.late === 0) return null;
  const { late, more } = count.data;
  return (
    <>
      <span aria-hidden className="bg-danger text-surface ml-auto rounded-full px-2 text-xs">
        {late}
        {more ? "+" : ""}
      </span>
      <span className="sr-only">, {t("overdueCount", { count: late })}</span>
    </>
  );
}
