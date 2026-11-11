"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useTimeZone, useTranslations } from "next-intl";
import { useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import { getTasksListQueryKey, tasksList } from "@/lib/api/client";
import type { PaginatedTaskList, Task, TasksListParams } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import { TaskForm } from "./task-form";
import { TaskRow } from "./task-row";
import { dayRange, isOpen, isOverdue } from "./task-time";

// Abiertas primero, por fecha de vencimiento (las sin fecha al final) y, a igualdad, por creación
// (el identificador es un UUIDv7, ADR-004): la API pagina por creación, no por fecha.
const byDue = (a: Task, b: Task) =>
  Number(isOpen(b)) - Number(isOpen(a)) ||
  Date.parse(a.due_at ?? "9999-12-31") - Date.parse(b.due_at ?? "9999-12-31") ||
  (a.id < b.id ? -1 : 1);

// Una lista de tareas (F5-08c) con los filtros de `params`: carga, vacío, error con reintento,
// sin permiso, «Cargar más» (que si falla no borra lo ya cargado) y «Editar» en cada fila abierta
// si hay `tasks.update`. `keep` descarta en el navegador lo que la API no sabe distinguir (las de
// todo el día de hoy no están vencidas todavía). Quién ve qué lo decide la API.
export function TasksList({
  params,
  label,
  empty,
  links,
  keep,
  now,
}: {
  params: TasksListParams;
  label: string;
  empty: string;
  links: boolean;
  keep?: "overdue";
  now: number;
}) {
  const t = useTranslations("tasks");
  const zone = useTimeZone() ?? "America/Lima";
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const { failed, retry } = useReads();
  const [editing, setEditing] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const { start } = dayRange(now, zone);
  const list = useInfiniteQuery<PaginatedTaskList, ApiError>({
    queryKey: [...getTasksListQueryKey(slug, params), "pages"],
    queryFn: ({ pageParam, signal }) =>
      tasksList(
        slug,
        { ...params, ...(pageParam ? { cursor: pageParam as string } : {}) },
        { signal },
      ),
    initialPageParam: "",
    getNextPageParam: (last) => last.next ?? undefined,
    gcTime: 0, // al salir no queda en memoria: cada visita pregunta a la API
  });
  const canEdit = permissions.some((grant) => grant.code === "tasks.update");
  const rows = (list.data?.pages.flatMap((page) => page.results) ?? [])
    .filter((task) => keep !== "overdue" || isOverdue(task, start, now))
    .sort(byDue);
  const denied = list.error?.status === 403;
  const problem = failed(list, t("denied"));

  if (denied && !list.data) return <p role="alert">{t("denied")}</p>; // una página siguiente no borra lo cargado
  if (!list.data && problem) {
    return (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {problem}
        </p>
        {retry(list)}
      </div>
    );
  }
  if (!list.data) {
    return (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }
  return (
    <>
      <p role="status" className={saved ? "text-sm" : "sr-only"}>
        {saved}
      </p>
      {rows.length === 0 ? <p className="text-muted">{empty}</p> : null}
      {rows.length > 0 ? (
        <ul aria-label={label} className="flex flex-col gap-2">
          {rows.map((task) =>
            editing === task.id ? (
              <li key={task.id}>
                <TaskForm
                  task={task}
                  onCancel={() => setEditing(null)}
                  onSaved={() => {
                    setEditing(null);
                    setSaved(t("form.saved"));
                  }}
                />
              </li>
            ) : (
              <TaskRow
                key={task.id}
                task={task}
                zone={zone}
                links={links}
                late={isOverdue(task, start, now)}
                onEdit={canEdit ? () => (setSaved(""), setEditing(task.id)) : undefined}
              />
            ),
          )}
        </ul>
      ) : null}
      {list.hasNextPage ? (
        <div className="flex flex-col items-start gap-2">
          <Button
            variant="accent"
            size="lg"
            aria-disabled={list.isFetchingNextPage}
            onClick={() => list.isFetchingNextPage || void list.fetchNextPage()}
          >
            {list.isFetchingNextPage ? t("loadingMore") : t("more")}
          </Button>
          {list.isFetchNextPageError && problem ? (
            <p role="alert" className="text-danger text-sm">
              {problem}
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
