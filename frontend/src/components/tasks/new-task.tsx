"use client";

import { useTimeZone, useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";

import { TaskForm, type TaskLink } from "./task-form";
import { fieldsOf } from "./task-time";

// «Nueva tarea» (F5-08c): botón, formulario y aviso, solo con `tasks.create` (la API lo vuelve a
// comprobar). Desde «Mis tareas» la fecha trae hoy (`withToday`); `link` deja la tarea vinculada.
// Al cerrar, el foco vuelve al botón.
export function NewTask({ link, withToday }: { link?: TaskLink; withToday?: boolean }) {
  const t = useTranslations("tasks");
  const zone = useTimeZone() ?? "America/Lima";
  const { permissions } = useTenant();
  const [creating, setCreating] = useState(false);
  const [said, setSaid] = useState("");
  const [today, setToday] = useState<string>(); // el día en que se abrió el formulario
  const trigger = useRef<HTMLButtonElement>(null);
  const close = () => {
    setCreating(false);
    setTimeout(() => trigger.current?.focus(), 0);
  };
  if (!permissions.some((grant) => grant.code === "tasks.create")) return null;
  return (
    <div className="flex flex-col items-start gap-3">
      {creating ? (
        <TaskForm
          link={link}
          today={today}
          onCancel={close}
          onSaved={(task) => {
            setSaid(t(task.due_at ? "form.created" : "form.createdUndated"));
            close();
          }}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => {
            setSaid("");
            setToday(withToday ? fieldsOf(Date.now(), zone).date : undefined);
            setCreating(true);
          }}
        >
          {t("form.new")}
        </Button>
      )}
      <p role="status" className={said ? "text-sm" : "sr-only"}>
        {said}
      </p>
    </div>
  );
}
