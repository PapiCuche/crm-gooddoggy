"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTimeZone, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { getTasksListQueryKey, useTasksCreate, useTasksUpdate } from "@/lib/api/client";
import type { PriorityEnum, Task, TaskTypeEnum } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

import { fieldsOf, instantAt } from "./task-time";

const TYPES = [
  "CALL",
  "MESSAGE",
  "MEETING",
  "FOLLOW_UP",
  "QUOTE",
  "COLLECTION",
  "DELIVERY",
  "OTHER",
];
const PRIORITIES = ["LOW", "MEDIUM", "HIGH", "URGENT"];
export type TaskLink = { contact_id?: string; opportunity_id?: string };

// Crear o cambiar una tarea (F5-08c) con `POST …/tasks/` o `PATCH …/tasks/{id}/` (F5-08b2). La
// fecha es opcional: con ella, «Todo el día» envía el instante de las 00:00 de ese día en la zona
// de la pantalla y sin él, el de la hora elegida. Cambiar manda la `version` leída; un 409 se
// explica en lugar de pisar lo de otra persona. El responsable es quien crea (se reasigna aparte)
// y el vínculo (`link`) no se cambia. Lo escrito se conserva si la API rechaza; Escape cierra.
export function TaskForm({
  task,
  link,
  today,
  onSaved,
  onCancel,
}: {
  task?: Task;
  link?: TaskLink;
  today?: string; // la fecha que trae el formulario nuevo (YYYY-MM-DD)
  onSaved: (saved: Task) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("tasks");
  const errors = useTranslations("errors.api");
  const zone = useTimeZone() ?? "America/Lima";
  const slug = useTenant().organization.slug;
  const client = useQueryClient();
  const due = task?.due_at ? fieldsOf(Date.parse(task.due_at), zone) : null;
  const [title, setTitle] = useState(task?.title ?? "");
  const [type, setType] = useState<string>(task?.type ?? "FOLLOW_UP");
  const [priority, setPriority] = useState<string>(task?.priority ?? "MEDIUM");
  const [description, setDescription] = useState(task?.description ?? "");
  const [date, setDate] = useState(due?.date ?? today ?? "");
  const [allDay, setAllDay] = useState(task ? task.is_all_day : true);
  const [time, setTime] = useState(due?.time ?? "");
  const [asked, setAsked] = useState(false); // intentó enviar con algo que falta
  const id = useId();
  const titleInput = useRef<HTMLInputElement>(null);
  const sending = useRef(false);

  const options = { mutation: { networkMode: "always" as const } };
  const create = useTasksCreate(options);
  const update = useTasksUpdate(options);
  const write = task ? update : create;
  const busy = write.isPending || (write.isError && write.error?.status === 401);
  const error = write.isError && write.error?.status !== 401 ? write.error : null;
  const bad = (field: string) =>
    error?.code === "VALIDATION_ERROR" && Object.hasOwn(error.fields ?? {}, field);
  const fieldError = {
    title:
      (asked && !title.trim() && t("form.titleRequired")) ||
      (bad("title") && t("form.titleInvalid")),
    time: asked && !allDay && date && !time && t("form.timeRequired"),
    description: bad("description") && t("form.descriptionInvalid"),
    date: bad("due_at") && t("form.dueInvalid"),
  };
  let formError: string | null = null;
  if (error && !bad("title") && !bad("description") && !bad("due_at")) {
    if (bad("contact_id") || bad("opportunity_id")) formError = t("form.linkGone");
    else if (error.code === "STALE_VERSION") formError = t("form.stale");
    else if (error.code === "TASK_CLOSED") formError = t("form.closed");
    else if (error.status === 404) formError = t("form.gone");
    else if (error.code === "PERMISSION_DENIED") formError = t("form.denied");
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  useEffect(() => {
    titleInput.current?.focus(); // al abrir, el foco va al primer campo
  }, []);
  useEffect(() => {
    if (!busy) sending.current = false;
  }, [busy]);

  function submit() {
    setAsked(true);
    if (sending.current || !title.trim() || (date && !allDay && !time)) return;
    let dueAt: string | null = null;
    if (date) {
      const [y, m, d] = date.split("-").map(Number) as [number, number, number];
      const [h, min] = allDay ? [0, 0] : (time.split(":").map(Number) as [number, number]);
      dueAt = new Date(instantAt(zone, y, m, d, h, min)).toISOString();
    }
    sending.current = true;
    const common = {
      ...{ title: title.trim(), type: type as TaskTypeEnum, priority: priority as PriorityEnum },
      ...{ description: description.trim(), due_at: dueAt, is_all_day: !!date && allDay },
    };
    const done = {
      onSuccess: (saved: Task) => {
        void client.invalidateQueries({ queryKey: getTasksListQueryKey(slug) });
        onSaved(saved);
      },
    };
    if (task)
      update.mutate(
        { orgSlug: slug, taskId: task.id, data: { ...common, version: task.version } },
        done,
      );
    else create.mutate({ orgSlug: slug, data: { ...common, ...link } }, done);
  }

  const area = cn(
    "bg-surface border-foreground/50 min-h-24 resize-y rounded-[10px] border px-3 py-2.5 text-[16px] leading-relaxed",
    "focus-visible:border-foreground aria-invalid:border-danger",
  );
  return (
    <form
      noValidate
      aria-label={t(task ? "form.edit" : "form.new")}
      aria-busy={busy}
      className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy && !event.nativeEvent.isComposing) onCancel();
        if (event.repeat && event.key === "Enter") event.preventDefault();
      }}
      onInput={() => write.isError && write.reset()}
    >
      <h2 className="font-medium">{t(task ? "form.edit" : "form.new")}</h2>
      <TextField
        ref={titleInput}
        label={t("form.title")}
        value={title}
        maxLength={200}
        autoComplete="off"
        error={fieldError.title || undefined}
        onChange={(event) => setTitle(event.target.value)}
      />
      <Choice
        label={t("form.type")}
        value={type}
        busy={busy}
        onChange={setType}
        options={TYPES.map((key): [string, string] => [key, t(`types.${key}`)])}
      />
      <div className="flex flex-col gap-1.5">
        <label htmlFor={`${id}-description`} className="text-sm font-medium">
          {t("form.description")}
        </label>
        <textarea
          id={`${id}-description`}
          rows={3}
          value={description}
          aria-invalid={fieldError.description ? true : undefined}
          aria-describedby={`${id}-description-note`}
          readOnly={busy}
          className={area}
          onChange={(event) => setDescription(event.target.value)}
        />
        <p
          id={`${id}-description-note`}
          role={fieldError.description ? "alert" : undefined}
          className={cn("text-sm", fieldError.description ? "text-danger" : "text-muted")}
        >
          {fieldError.description || t("form.descriptionHint")}
        </p>
      </div>
      <Choice
        label={t("form.priority")}
        value={priority}
        busy={busy}
        onChange={setPriority}
        options={PRIORITIES.map((key): [string, string] => [key, t(`priorities.${key}`)])}
      />
      <TextField
        type="date"
        label={t("form.date")}
        value={date}
        hint={t("form.dateHint", { zone })}
        error={fieldError.date || undefined}
        onChange={(event) => setDate(event.target.value)}
      />
      {date ? (
        <>
          <label className="flex min-h-11 items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={allDay}
              onChange={(event) => setAllDay(event.target.checked)}
            />
            {t("form.allDay")}
          </label>
          {allDay ? null : (
            <TextField
              type="time"
              label={t("form.time")}
              value={time}
              error={fieldError.time || undefined}
              onChange={(event) => setTime(event.target.value)}
            />
          )}
        </>
      ) : null}
      {formError ? (
        <p role="alert" className="text-danger text-sm">
          {formError}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="ghost"
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
          onClick={() => busy || onCancel()}
        >
          {t("form.cancel")}
        </Button>
        <Button
          type="submit"
          variant="primary"
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
        >
          {busy ? t("form.busy") : t("form.submit")}
        </Button>
      </div>
    </form>
  );
}
