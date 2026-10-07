"use client";

import { useFormatter, useTranslations } from "next-intl";
import { type Ref, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { CursorList } from "@/components/lists/cursor-list";
import { Button } from "@/components/ui/button";
import { auditList, getAuditListQueryKey } from "@/lib/api/client";
import type {
  AuditActorTypeEnum,
  AuditEntry,
  AuditListParams,
  AuditResultEnum,
} from "@/lib/api/model";
import { text } from "@/lib/message-text";
import { cn } from "@/lib/utils";

import messages from "../../../messages/es-PE.json";

// Tipos de actor y resultados son enums del contrato: si gana uno y el catálogo no, no compila.
// Uno que esta versión no conoce (una API más nueva) se enseña con su código, como una acción.
const ACTORS: Record<AuditActorTypeEnum, string> = messages.audit.actor;
const RESULTS: Record<AuditResultEnum, string> = messages.audit.result;
// Y lo que ni siquiera es un texto (una API que no cumple el contrato), como texto: no rompe.
const named = (catalog: unknown, code: unknown) => text(catalog, String(code)) ?? String(code);
// Acciones y tipos de entidad son texto libre de la API: se buscan por propiedades propias
// (`role.created` es `actions.role.created`), nunca como ruta de mensajes, y lo que no tiene
// nombre se enseña con su código.
const { actions: ACTIONS, entities: ENTITIES } = messages.audit;

type Filters = { action: string; entity_type: string; actor_type: AuditActorTypeEnum | "" };
const NONE: Filters = { action: "", entity_type: "", actor_type: "" };
type Options = readonly (readonly [code: string, name: string])[];
// Lo que cada selector ofrece: lo que esta pantalla sabe nombrar, en el orden del catálogo.
const ACTION_OPTIONS: Options = Object.entries(ACTIONS).flatMap(([module, names]) =>
  Object.entries(names).map(([action, name]) => [`${module}.${action}`, name] as const),
);
const ENTITY_OPTIONS: Options = Object.entries(ENTITIES);
// «una persona» es «Una persona» cuando encabeza una opción.
const ACTOR_OPTIONS: Options = Object.entries(ACTORS).map(
  ([code, name]) => [code, name.charAt(0).toUpperCase() + name.slice(1)] as const,
);

// Un filtro: su etiqueta y un selector con «todo» y lo que se puede elegir.
function Choice({
  label,
  all,
  value,
  options,
  onChange,
  ref,
}: {
  label: string;
  all: string;
  value: string;
  options: Options;
  onChange: (value: string) => void;
  ref?: Ref<HTMLSelectElement>;
}) {
  const id = useId();
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {/* 16 px en el control: por debajo, Safari en iOS amplía la página al enfocarlo. */}
      <select
        ref={ref}
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border px-3 text-[16px]"
      >
        <option value="">{all}</option>
        {options.map(([code, name]) => (
          <option key={code} value={code}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}

// Auditoría (F2-75): lo que devuelve `GET /api/v1/o/{slug}/audit/`, de lo más reciente a lo más
// antiguo. La lista, sus estados y su foco son los de `CursorList`. Quién puede verla lo decide
// la API. `changes` y `metadata` no se enseñan todavía (OBS-F2-73-1).
export function AuditList() {
  const t = useTranslations("audit");
  const format = useFormatter();
  const { organization } = useTenant();
  // Filtros (F2-76): lo elegido en cada selector; vacío es «sin filtro» y no se envía (la API
  // responde 400 a un filtro vacío). Los valores salen de las listas de esta pantalla.
  const [chosen, setChosen] = useState<Filters>(NONE);
  const first = useRef<HTMLSelectElement>(null);
  const filters: AuditListParams = {
    ...(chosen.action ? { action: chosen.action } : {}),
    ...(chosen.entity_type ? { entity_type: chosen.entity_type } : {}),
    ...(chosen.actor_type ? { actor_type: chosen.actor_type } : {}),
  };
  const filtered = Object.keys(filters).length > 0;
  const choose = (name: keyof Filters) => (value: string) =>
    setChosen((before) => ({ ...before, [name]: value }));

  return (
    <CursorList<AuditEntry>
      section="audit"
      organization={organization.name}
      // Los filtros van en la clave: cada combinación es su propia lista, pedida de nuevo.
      listKey={[...getAuditListQueryKey(organization.slug), "pages", filters]}
      fetchPage={(cursor, signal) =>
        auditList(organization.slug, { ...filters, ...(cursor ? { cursor } : {}) }, { signal })
      }
      controls={
        <div role="group" aria-label={t("filters.title")} className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <Choice
              ref={first}
              label={t("filters.action")}
              all={t("filters.allActions")}
              value={chosen.action}
              options={ACTION_OPTIONS}
              onChange={choose("action")}
            />
            <Choice
              label={t("filters.entity")}
              all={t("filters.allEntities")}
              value={chosen.entity_type}
              options={ENTITY_OPTIONS}
              onChange={choose("entity_type")}
            />
            <Choice
              label={t("filters.actor")}
              all={t("filters.anyActor")}
              value={chosen.actor_type}
              options={ACTOR_OPTIONS}
              onChange={choose("actor_type")}
            />
          </div>
          {filtered ? (
            <Button
              variant="ghost"
              className="border-border min-h-11 self-start border sm:min-h-9"
              onClick={() => {
                setChosen(NONE);
                first.current?.focus(); // el botón desaparece: el foco, al primer selector
              }}
            >
              {t("filters.clear")}
            </Button>
          ) : null}
        </div>
      }
      empty={<p className="text-muted">{t(filtered ? "filters.none" : "empty")}</p>}
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
                  actor: named(ACTORS, entry.actor_type),
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
                {named(RESULTS, entry.result)}
              </span>
            </p>
          </>
        );
      }}
    </CursorList>
  );
}
