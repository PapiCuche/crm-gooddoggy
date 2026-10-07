"use client";

import { useFormatter, useTranslations } from "next-intl";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { auditList, getAuditListQueryKey } from "@/lib/api/client";
import type { AuditActorTypeEnum, AuditEntry, AuditResultEnum } from "@/lib/api/model";
import { text } from "@/lib/message-text";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";

// Tipos de actor y resultados son enums del contrato: si gana uno y el catálogo no, no compila.
// Uno que esta versión no conoce (una API más nueva) se enseña con su código, como una acción.
const ACTORS: Record<AuditActorTypeEnum, string> = messages.audit.actor;
const RESULTS: Record<AuditResultEnum, string> = messages.audit.result;
// Acciones y tipos de entidad son texto libre de la API: se buscan por propiedades propias
// (`role.created` es `actions.role.created`), nunca como ruta de mensajes, y lo que no tiene
// nombre se enseña con su código.
const { actions: ACTIONS, entities: ENTITIES } = messages.audit;

// Auditoría (F2-75): lo que devuelve `GET /api/v1/o/{slug}/audit/`, de lo más reciente a lo más
// antiguo. La lista, sus estados y su foco son los de `CursorList`. Quién puede verla lo decide
// la API. `changes` y `metadata` no se enseñan todavía (OBS-F2-73-1).
export function AuditList() {
  const t = useTranslations("audit");
  const format = useFormatter();
  const { organization } = useTenant();

  return (
    <CursorList<AuditEntry>
      section="audit"
      organization={organization.name}
      listKey={[...getAuditListQueryKey(organization.slug), "pages"]}
      fetchPage={(cursor, signal) =>
        auditList(organization.slug, cursor ? { cursor } : undefined, { signal })
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-2 sm:grid-cols-[minmax(0,3fr)_minmax(0,3fr)_minmax(0,2fr)] sm:items-start"
    >
      {(entry) => {
        const action = text(ACTIONS, entry.action);
        return (
          <>
            <p className="flex min-w-0 flex-col leading-snug">
              <span className="font-medium wrap-anywhere">{action ?? entry.action}</span>
              {action ? (
                <span className="text-muted font-mono text-sm wrap-anywhere">{entry.action}</span>
              ) : null}
            </p>
            <div className="flex min-w-0 flex-col text-sm">
              <p className="wrap-anywhere">
                {t(entry.entity_label ? "entityNamed" : "entity", {
                  type: text(ENTITIES, entry.entity_type) ?? entry.entity_type,
                  label: entry.entity_label ?? "",
                })}
              </p>
              <p className="text-muted wrap-anywhere">
                {t(entry.actor_label ? "byNamed" : "by", {
                  actor: text(ACTORS, entry.actor_type) ?? entry.actor_type,
                  label: entry.actor_label ?? "",
                })}
              </p>
            </div>
            <p className="flex flex-col text-sm sm:items-end sm:text-right">
              <time dateTime={entry.occurred_at}>
                {format.dateTime(new Date(entry.occurred_at), {
                  dateStyle: "medium",
                  timeStyle: "medium",
                })}
              </time>
              <span
                className={cn(
                  "font-medium wrap-anywhere",
                  entry.result === "SUCCESS" ? "text-muted" : "text-danger",
                )}
              >
                {text(RESULTS, entry.result) ?? entry.result}
              </span>
            </p>
          </>
        );
      }}
    </CursorList>
  );
}
