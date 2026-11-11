"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { useOwners } from "@/components/deals/use-deals";
import { Button } from "@/components/ui/button";
import {
  getTasksListQueryKey,
  tasksAssign,
  tasksCancel,
  tasksComplete,
  tasksReopen,
} from "@/lib/api/client";
import type { Task } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { isOpen } from "./task-time";

type Run = { call: () => Promise<Task>; done: string };
const GHOST = "border-border min-h-11 border sm:min-h-9";

// Lo que se hace con una tarea (F5-08c): editar, completar, cancelar con motivo, reasignar y
// reabrir, según su estado y los permisos de la sesión (`tasks.update`, `tasks.assign`; la API los
// vuelve a comprobar). Cada acción manda la `version` leída: un 409 se explica en lugar de pisar el
// cambio de otra persona. Cancelar y reasignar abren su pequeño formulario; Escape lo cierra y el
// foco vuelve a su botón. Una sola escritura a la vez por tarea. `onDone` anuncia el resultado.
export function TaskActions({
  task,
  onEdit,
  onDone,
}: {
  task: Task;
  onEdit: () => void;
  onDone: (text: string) => void;
}) {
  const t = useTranslations("tasks");
  const errors = useTranslations("errors.api");
  const { organization, permissions, user } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const owners = useOwners();
  const held = (code: string) => permissions.some((grant) => grant.code === code);
  const [panel, setPanel] = useState<"cancel" | "assign" | null>(null);
  const [reason, setReason] = useState("");
  const [assignee, setAssignee] = useState(task.assigned_user_id ?? user.id);
  // La versión que se leyó al abrir el formulario: la lista se vuelve a pedir mientras se escribe
  // y `task` puede traer la de otra persona, cuyo cambio quien decide aquí no ha visto.
  const [opened, setOpened] = useState(task.version);
  const buttons = useRef<Partial<Record<string, HTMLButtonElement | null>>>({});
  const form = useRef<HTMLFormElement>(null);
  const id = useId();
  const sending = useRef(false); // dos pulsaciones seguidas no son dos escrituras
  const act = useMutation<Task, ApiError, Run>({
    networkMode: "always",
    mutationFn: ({ call }) => call(),
    onSettled: () => {
      sending.current = false;
    },
    onError: async (error) => {
      // Lo mostrado ya no vale (otra persona cambió o borró la tarea): se vuelve a pedir la lista
      // y, con la fila ya fuera (404), se dice en el aviso de la lista, que no se va con ella.
      if (error.status === 404 || error.status === 409) {
        await client.invalidateQueries({ queryKey: getTasksListQueryKey(slug) });
      }
      if (error.status === 404) onDone(t("form.gone"));
    },
    onSuccess: async (_, { done }) => {
      await client.invalidateQueries({ queryKey: getTasksListQueryKey(slug) });
      setPanel(null);
      onDone(done);
    },
  });
  const busy = act.isPending || (act.isError && act.error.status === 401);
  const run = (done: string, call: () => Promise<Task>) => {
    if (busy || sending.current) return;
    sending.current = true;
    act.mutate({ done, call });
  };
  const version = { version: task.version }; // las acciones de un clic usan la de ese momento
  const open = isOpen(task);

  const bad = (name: string) =>
    act.error?.code === "VALIDATION_ERROR" && Object.hasOwn(act.error.fields ?? {}, name);
  let problem: string | null = null;
  const error = act.isError && act.error.status !== 401 ? act.error : null;
  if (error) {
    if (bad("assigned_user_id")) problem = t("actions.assigneeInvalid");
    else if (bad("reason")) problem = t("actions.reasonInvalid");
    else if (error.code === "STALE_VERSION") problem = t(panel ? "form.stale" : "actions.stale");
    else if (error.code === "TASK_CLOSED") problem = t("actions.closed");
    else if (error.code === "TASK_OPEN") problem = t("actions.isOpen");
    else if (error.status === 404)
      problem = null; // lo dice el aviso de la lista
    else if (error.code === "PERMISSION_DENIED") problem = t("actions.denied");
    else
      problem = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  useEffect(() => {
    form.current?.querySelector<HTMLElement>("textarea, select")?.focus(); // al abrir, al primer control
  }, [panel]);
  function show(next: "cancel" | "assign" | null) {
    act.reset();
    if (next) {
      setOpened(task.version);
      setAssignee(task.assigned_user_id ?? user.id);
    }
    setPanel(next);
    if (!next && panel) setTimeout(() => buttons.current[panel]?.focus(), 0);
  }
  const button = (key: string, label: string, onClick: () => void) => (
    <Button
      key={key}
      ref={(node) => {
        buttons.current[key] = node;
      }}
      variant="ghost"
      className={GHOST}
      aria-disabled={busy}
      aria-expanded={key === "cancel" || key === "assign" ? panel === key : undefined}
      aria-label={t(`actions.${key}Label`, { title: task.title })}
      onClick={() => busy || onClick()}
    >
      {label}
    </Button>
  );
  const owner = task.assigned_user_id;
  const choices: [string, string][] = [
    [user.id, owners.name(user.id)],
    // quien la tiene, aunque no se pueda leer el directorio
    ...(owner && owner !== user.id && !owners.options.some(([other]) => other === owner)
      ? [[owner, owners.name(owner)] as [string, string]]
      : []),
    ...owners.options,
  ];

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {open && held("tasks.update") ? button("edit", t("row.edit"), onEdit) : null}
        {open && held("tasks.update")
          ? button("complete", t("actions.complete"), () =>
              run(t("actions.completed"), () => tasksComplete(slug, task.id, version)),
            )
          : null}
        {open && held("tasks.update")
          ? button("cancel", t("actions.cancel"), () => show(panel === "cancel" ? null : "cancel"))
          : null}
        {open && held("tasks.assign")
          ? button("assign", t("actions.assign"), () => show(panel === "assign" ? null : "assign"))
          : null}
        {!open && held("tasks.update")
          ? button("reopen", t("actions.reopen"), () =>
              run(t("actions.reopened"), () => tasksReopen(slug, task.id, version)),
            )
          : null}
      </div>
      {panel ? (
        <form
          ref={form}
          noValidate
          aria-label={t(`actions.${panel}Form`)}
          aria-busy={busy}
          className="border-border flex max-w-xl flex-col gap-3 rounded-lg border p-3"
          onKeyDown={(event) => {
            if (event.key === "Escape" && !busy && !event.nativeEvent.isComposing) show(null);
          }}
          onSubmit={(event) => {
            event.preventDefault();
            if (panel === "cancel") {
              const data = { version: opened, ...(reason.trim() ? { reason: reason.trim() } : {}) };
              run(t("actions.cancelled"), () => tasksCancel(slug, task.id, data));
            } else {
              run(t("actions.assigned"), () =>
                tasksAssign(slug, task.id, { version: opened, assigned_user_id: assignee }),
              );
            }
          }}
        >
          {panel === "cancel" ? (
            <div className="flex flex-col gap-1.5">
              <label htmlFor={`${id}-reason`} className="text-sm font-medium">
                {t("actions.reason")}
              </label>
              <textarea
                id={`${id}-reason`}
                rows={3}
                value={reason}
                readOnly={busy}
                aria-invalid={bad("reason") ? true : undefined}
                className={cn(
                  "bg-surface border-foreground/50 min-h-20 resize-y rounded-[10px] border px-3 py-2.5 text-[16px]",
                  "focus-visible:border-foreground aria-invalid:border-danger",
                )}
                onChange={(event) => setReason(event.target.value)}
              />
            </div>
          ) : (
            <Choice
              label={t("actions.assignee")}
              value={assignee}
              busy={busy}
              onChange={setAssignee}
              options={choices}
            />
          )}
          {problem ? (
            <p role="alert" className="text-danger text-sm">
              {problem}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => busy || show(null)}
            >
              {t("actions.back")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
            >
              {busy ? t("actions.busy") : t(`actions.${panel}Confirm`)}
            </Button>
          </div>
        </form>
      ) : null}
      {!panel && problem ? (
        <p role="alert" className="text-danger text-sm">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
