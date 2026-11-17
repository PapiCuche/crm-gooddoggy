"use client";

import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import {
  useQuickRepliesCreate,
  useQuickRepliesUpdate,
  useQuickReplyCategoriesCreate,
  useQuickReplyCategoriesUpdate,
} from "@/lib/api/client";
import {
  type ManagedQuickReplyCategory,
  type QuickReply,
  QuickReplyVariableEnum,
} from "@/lib/api/model";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

// Lo que la pantalla ofrece a sus formularios y acciones: recargar y anunciar, o explicar un fallo
// (409 `STALE_VERSION` o 404: recarga y lo dice).
export type Ctx = {
  slug: string;
  reload: () => void;
  clear: () => void;
  say: (text: string) => void;
  after: (text: string) => Promise<void>;
  fail: (error: ApiError, name: string) => void;
};

const SHORTCUT = /^\/?[a-z0-9_-]{1,32}$/i; // la `/` inicial se acepta y no se envía
const ORDER = /^-?\d{1,5}$/;
const TOKEN = /\{\{\s*([^{}\s]*)\s*\}\}/g; // como el resolvedor de la API
const VARIABLES = Object.values(QuickReplyVariableEnum);
const AREA =
  "bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger rounded-[10px] border px-3 text-[16px]";
const changed = <T extends object>(wanted: T, base: T) =>
  Object.fromEntries(Object.entries(wanted).filter(([k, v]) => v !== base[k as keyof T]));
const gone = (error: ApiError) => error.status === 404 || error.code === "STALE_VERSION";

// Un botón que abre un formulario en su sitio; al cerrarse, el foco vuelve al botón.
export function Editor(props: {
  label: string;
  aria?: string;
  accent?: boolean;
  onOpen: () => void;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const was = useRef(false);
  useEffect(() => {
    if (!open && was.current && document.activeElement === document.body) trigger.current?.focus();
    was.current = open;
  }, [open]);
  if (open) return <div className="w-full">{props.children(() => setOpen(false))}</div>;
  return (
    <Button
      ref={trigger}
      variant={props.accent ? "accent" : "ghost"}
      className={cn("min-h-11 sm:min-h-9", !props.accent && "border-border border")}
      aria-label={props.aria}
      onClick={() => {
        props.onOpen();
        setOpen(true);
      }}
    >
      {props.label}
    </Button>
  );
}

// Crear (sin `reply`) o editar una respuesta rápida (F6-08c): atajo, título, categoría y texto,
// con una vista previa resuelta con un cliente de ejemplo. Editar envía solo lo que cambió.
export function ReplyForm({
  ctx,
  reply,
  categories,
  close,
}: {
  ctx: Ctx;
  reply?: QuickReply;
  categories: readonly ManagedQuickReplyCategory[];
  close: () => void;
}) {
  const t = useTranslations("quickReplies.form");
  const api = useTranslations("errors.api");
  const { organization } = useTenant();
  const id = useId();
  const [text, setText] = useState(reply?.body ?? "");
  const [empty, setEmpty] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  const select = useRef<HTMLSelectElement>(null);
  const base = useRef(reply); // la fila al abrir: con ella se compara y se manda su `version`
  const mutation = {
    networkMode: "always" as const,
    onSuccess: async (row: QuickReply) => {
      await ctx.after(t("done", { name: row.shortcut, created: String(!reply) }));
      close();
    },
    onError: (error: ApiError) => {
      if (!reply || !gone(error)) return;
      close();
      ctx.fail(error, `/${reply.shortcut}`);
    },
  };
  const create = useQuickRepliesCreate({ mutation });
  const update = useQuickRepliesUpdate({ mutation });
  const write = reply ? update : create;
  const bad = write.error?.code === "VALIDATION_ERROR" ? (write.error.fields ?? {}) : {};
  const bodyError = empty ? t("bodyRequired") : "body" in bad ? t("bodyInvalid") : undefined;
  // Un `Map`, no un objeto: `{{constructor}}` no es una variable.
  const sample = new Map([
    ["contact.first_name", t("sampleFirst")],
    ["contact.name", t("sampleName")],
    ["org.name", organization.name],
  ]);
  const options = categories.filter((c) => c.is_active || c.id === reply?.category_id);
  const title = reply ? t("editLabel", { name: `/${reply.shortcut}` }) : t("title");

  return (
    <FieldsForm
      title={title}
      heading={<h3 className="font-medium wrap-anywhere">{title}</h3>}
      className="border-border bg-surface flex w-full flex-col gap-4 rounded-lg border p-4"
      fields={[
        {
          ...{ name: "shortcut", label: t("shortcut"), max: 33, literal: true },
          ...{ required: t("shortcutRequired"), invalid: t("shortcutInvalid") },
          ...{ hint: t("shortcutHint"), valid: (v: string) => SHORTCUT.test(v) },
          defaultValue: reply?.shortcut,
        },
        {
          ...{ name: "title", label: t("name"), max: 100, defaultValue: reply?.title },
          ...{ required: t("nameRequired"), invalid: t("nameInvalid") },
        },
      ]}
      note={
        <>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${id}-c`} className="text-sm font-medium">
              {t("category")}
            </label>
            <select
              ref={select}
              id={`${id}-c`}
              defaultValue={reply?.category_id ?? options[0]?.id}
              aria-invalid={"category_id" in bad || undefined}
              className={cn(AREA, "h-11")}
            >
              {options.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            {"category_id" in bad ? (
              <p className="text-danger text-sm">{t("categoryInvalid")}</p>
            ) : null}
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor={`${id}-b`} className="text-sm font-medium">
              {t("body")}
            </label>
            <textarea
              ref={area}
              id={`${id}-b`}
              rows={4}
              maxLength={4096}
              value={text}
              onChange={(event) => {
                setText(event.target.value);
                setEmpty(false);
              }}
              aria-invalid={bodyError ? true : undefined}
              aria-describedby={`${id}-n`}
              className={cn(AREA, "min-h-28 resize-y py-2.5 leading-relaxed")}
            />
            <p
              id={`${id}-n`}
              role={bodyError ? "alert" : undefined}
              className={cn("text-sm", bodyError ? "text-danger" : "text-muted")}
            >
              {bodyError ??
                t("bodyHint", { variables: VARIABLES.map((v) => `{{${v}}}`).join(", ") })}
            </p>
          </div>
          <div className="bg-surface-raised flex flex-col gap-1 rounded-lg p-3 text-sm">
            <p className="text-muted">{t("preview")}</p>
            <p className="wrap-anywhere whitespace-pre-wrap">
              {text.replace(TOKEN, (all, key: string) => sample.get(key) ?? all)}
            </p>
          </div>
        </>
      }
      write={write}
      check={() => {
        if (text.trim()) return true;
        setEmpty(true);
        area.current?.focus();
        return false;
      }}
      send={({ shortcut, title: name }) => {
        const wanted = {
          ...{ shortcut: shortcut.replace(/^\//, "").toLowerCase(), title: name },
          ...{ category_id: select.current!.value, body: text },
        };
        if (!reply) return create.mutate({ orgSlug: ctx.slug, data: wanted });
        const data = changed(wanted, base.current!);
        if (Object.keys(data).length === 0) {
          close();
          return ctx.say(t("unchanged", { name: reply.shortcut }));
        }
        const version = base.current!.version;
        update.mutate({ orgSlug: ctx.slug, quickReplyId: reply.id, data: { ...data, version } });
      }}
      taken={(error) =>
        error.code === "SHORTCUT_TAKEN"
          ? { field: "shortcut", text: t("shortcutTaken") }
          : ["body", "category_id"].some((key) => Object.hasOwn(error.fields ?? {}, key))
            ? { text: api("VALIDATION_ERROR") }
            : null
      }
      denied={t("denied")}
      labels={{ submit: t(reply ? "save" : "create"), busy: t("busy"), cancel: t("cancel") }}
      onCancel={close}
    />
  );
}

// Crear (sin `category`) o renombrar y reordenar una categoría (F6-08c2). El orden es un entero, de
// menor a mayor: cambiarlo es un solo PATCH de esta categoría, sin tocar las demás.
export function CategoryForm({
  ctx,
  category,
  close,
}: {
  ctx: Ctx;
  category?: ManagedQuickReplyCategory;
  close: () => void;
}) {
  const t = useTranslations("quickReplies.categoryForm");
  const base = useRef(category);
  const mutation = {
    networkMode: "always" as const,
    onSuccess: async (row: ManagedQuickReplyCategory) => {
      await ctx.after(t("done", { name: row.name, created: String(!category) }));
      close();
    },
    onError: (error: ApiError) => {
      if (error.code === "CATEGORY_NAME_TAKEN") return ctx.reload(); // la otra, a la vista
      if (!category || !gone(error)) return;
      close();
      ctx.fail(error, category.name);
    },
  };
  const create = useQuickReplyCategoriesCreate({ mutation });
  const update = useQuickReplyCategoriesUpdate({ mutation });
  const write = category ? update : create;
  const title = category ? t("editLabel", { name: category.name }) : t("title");
  const fits = (v: string) => ORDER.test(v) && Number(v) >= -32768 && Number(v) <= 32767;

  return (
    <FieldsForm
      title={title}
      heading={<h3 className="font-medium wrap-anywhere">{title}</h3>}
      className="border-border bg-surface flex w-full flex-col gap-4 rounded-lg border p-4"
      fields={[
        {
          ...{ name: "name", label: t("name"), max: 100, defaultValue: category?.name },
          ...{ required: t("nameRequired"), invalid: t("nameInvalid") },
        },
        {
          ...{ name: "sort_order", label: t("order"), max: 6 }, // sin `inputMode`: iOS no da el signo menos
          ...{ invalid: t("orderInvalid"), valid: fits },
          ...(category
            ? { required: t("orderRequired"), defaultValue: String(category.sort_order) }
            : { hint: t("orderHint") }),
        },
      ]}
      write={write}
      send={({ name, sort_order: order }) => {
        const wanted = { name, ...(order ? { sort_order: Number(order) } : {}) };
        if (!category) return create.mutate({ orgSlug: ctx.slug, data: wanted });
        const data = changed(wanted, base.current!);
        if (Object.keys(data).length === 0) {
          close();
          return ctx.say(t("unchanged", { name: category.name }));
        }
        const version = base.current!.version;
        update.mutate({ orgSlug: ctx.slug, categoryId: category.id, data: { ...data, version } });
      }}
      taken={(error) =>
        error.code === "CATEGORY_NAME_TAKEN" ? { field: "name", text: t("nameTaken") } : null
      }
      denied={t("denied")}
      labels={{ submit: t(category ? "save" : "create"), busy: t("busy"), cancel: t("cancel") }}
      onCancel={close}
    />
  );
}
