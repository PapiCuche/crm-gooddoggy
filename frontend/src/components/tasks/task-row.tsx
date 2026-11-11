import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Plain } from "@/components/inbox/plain";
import { Button } from "@/components/ui/button";
import type { Task } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { isOpen } from "./task-time";

// Una tarea en una lista (F5-08c). Todo texto que escribió otra persona (título, descripción,
// nombre del cliente) va como texto plano. El nombre del cliente llega `null` si quien consulta
// no puede ver ese contacto (D-F6-10; un Supervisor ve tareas de su equipo con clientes que no
// son suyos): se dice sin nombre y sin enlace a una ficha que tampoco abriría.
export function TaskRow({
  task,
  late,
  zone,
  links,
  onEdit,
}: {
  task: Task;
  late: boolean; // vencida según la pantalla (la de todo el día cuenta al acabar su día)
  zone: string;
  links: boolean; // mostrar el cliente y la oportunidad (no donde ya son el contexto)
  onEdit?: () => void;
}) {
  const t = useTranslations("tasks");
  const format = useFormatter();
  const slug = useTenant().organization.slug;
  const date = task.due_at ? new Date(task.due_at) : null;
  const day = { weekday: "short", day: "numeric", month: "short", timeZone: zone } as const;
  const hour = { ...day, hour: "numeric", minute: "2-digit" } as const;
  let when = t("row.noDue");
  if (date && task.is_all_day) when = t("row.dueDay", { day: format.dateTime(date, day) });
  else if (date) when = t("row.dueAt", { when: format.dateTime(date, hour) });
  const { contact } = task;
  return (
    <li
      id={`task-${task.id}`}
      className={cn(
        "border-border bg-surface flex flex-col gap-2 rounded-lg border p-4 leading-snug",
        !isOpen(task) && "opacity-75",
      )}
    >
      <p className="font-medium">
        <Plain>{task.title}</Plain>
      </p>
      <p className="text-muted flex flex-wrap gap-x-3 gap-y-1 text-sm">
        <span>{t(`types.${task.type}`)}</span>
        <span>{t(`statuses.${task.status}`)}</span>
        <span>{t("row.priority", { priority: t(`priorities.${task.priority}`) })}</span>
        <span className={late ? "text-danger font-medium" : undefined}>
          {when}
          {late ? ` · ${t("row.overdue")}` : ""}
        </span>
      </p>
      {task.description ? (
        <p className="text-sm">
          <Plain>{task.description}</Plain>
        </p>
      ) : null}
      {links && contact.id ? (
        <p className="text-sm">
          {contact.display_name === null ? (
            t("row.contactHidden")
          ) : (
            <Link
              href={`/o/${slug}/clientes/${contact.id}`}
              prefetch={false}
              className="underline-offset-4 hover:underline"
            >
              {t("row.contact")} <Plain>{contact.display_name}</Plain>
            </Link>
          )}
        </p>
      ) : null}
      {links && task.opportunity_id ? (
        <p className="text-sm">
          <Link
            href={`/o/${slug}/oportunidades/${task.opportunity_id}`}
            prefetch={false}
            className="underline-offset-4 hover:underline"
          >
            {t("row.opportunity")}
          </Link>
        </p>
      ) : null}
      {onEdit && isOpen(task) ? (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="ghost"
            className="border-border min-h-11 border sm:min-h-9"
            aria-label={t("row.editLabel", { title: task.title })}
            onClick={onEdit}
          >
            {t("row.edit")}
          </Button>
        </div>
      ) : null}
    </li>
  );
}
