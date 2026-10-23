"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, type MouseEvent, useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useTagsCreate, useTagsUpdate } from "@/lib/api/client";
import type { Tag } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { inkOn, TagChip } from "./contact-tags";

const COLOR = /^#[0-9a-f]{6}$/i;
// La paleta que se ofrece: los colores del tema (globals.css). Cualquier otro #rrggbb también vale.
export const PALETTE = [
  ["honey", "#ffdb5b"],
  ["ink", "#202020"],
  ["green", "#1a7f37"],
  ["red", "#b42318"],
  ["gray", "#5c5c5c"],
  ["light", "#dedede"],
] as const;
const FRESH = PALETTE[0][1]; // una etiqueta nueva empieza en miel

// Crear una etiqueta de contactos (sin `tag`) o renombrarla y cambiarle el color (F4-12c), con
// `POST` y `PATCH …/tags/` (F4-11). Editar envía solo lo que cambió. El color se escribe
// (`#rrggbb`, la API lo guarda en minúsculas) o se elige de la paleta, y una muestra enseña la
// etiqueta como se verá. El camino de envío es el de `FieldsForm`. Quién puede lo decide la API
// (`settings.manage`).
export function TagForm(props: {
  slug: string;
  listKey: QueryKey;
  tag?: Tag;
  onAsk: () => void; // se abre el formulario: los avisos anteriores ya no aplican
  onDone: (text: string) => void;
  onStale: (name: string, here: boolean) => void; // `here`: el foco seguía en esta fila
}) {
  const { slug, listKey, tag } = props;
  const t = useTranslations("contactTags.form");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState({ name: "", color: "" });
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // La etiqueta tal como estaba al abrir: con ella se compara lo que cambió, no con la fila que
  // la lista trae después (otra persona pudo renombrarla mientras tanto, y no se le deshace).
  const base = useRef<Pick<Tag, "name" | "color"> | null>(null);

  async function saved(row: Tag) {
    await queryClient.invalidateQueries({ queryKey: listKey });
    const stale = queryClient.getQueryState(listKey)?.status === "error";
    setOpen(false);
    props.onDone(t(stale ? "doneStale" : "done", { name: row.name, created: String(!tag) }));
  }
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useTagsCreate({ mutation: { networkMode: "always", onSuccess: saved } });
  const update = useTagsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: saved,
      onError: (error) => {
        if (error.status !== 404 || !tag) return; // no es de la organización: lo dice la lista
        const active = document.activeElement;
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        setOpen(false);
        props.onStale(tag.name, here);
      },
    },
  });
  const write = tag ? update : create;

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve al botón que abrió, salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    write.reset();
    if (next) props.onAsk();
    setPreview({ name: tag?.name ?? "", color: tag?.color ?? FRESH });
    base.current = tag ? { name: tag.name, color: tag.color } : null;
    setOpen(next);
  }
  // La muestra sigue a lo escrito en los dos campos.
  function read(event: FormEvent<HTMLDivElement>) {
    const form = (event.target as HTMLInputElement).form;
    if (!form) return;
    const value = (name: string) => (form.elements.namedItem(name) as HTMLInputElement).value;
    setPreview({ name: value("name").trim(), color: value("color").trim() });
  }
  // Un color de la paleta se escribe en el campo, como si se tecleara (retira el error anterior).
  function pick(event: MouseEvent<HTMLButtonElement>, color: string) {
    const input = event.currentTarget.form?.elements.namedItem("color") as HTMLInputElement;
    input.value = color;
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const title = tag ? t("editLabel", { name: tag.name }) : t("title");
  const busy = write.isPending;
  const shown = COLOR.test(preview.color) ? preview.color.toLowerCase() : null;

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-3", open && "w-full")}
      onInput={read}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <FieldsForm
          title={title}
          heading={<h2 className="font-medium wrap-anywhere">{title}</h2>}
          className={cn(
            "flex w-full max-w-xl flex-col gap-4",
            !tag && "border-border bg-surface rounded-lg border p-4",
          )}
          fields={[
            {
              ...{ name: "name", label: t("name"), max: 50, defaultValue: tag?.name },
              ...{ required: t("nameRequired"), invalid: t("nameInvalid") },
            },
            {
              ...{ name: "color", label: t("color"), max: 16, literal: true },
              ...{ required: t("colorRequired"), invalid: t("colorInvalid"), hint: t("colorHint") },
              ...{ valid: (value: string) => COLOR.test(value), defaultValue: tag?.color ?? FRESH },
            },
          ]}
          note={
            <>
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-2 text-sm font-medium">{t("palette")}</legend>
                <div className="flex flex-wrap gap-2">
                  {PALETTE.map(([key, color]) => (
                    <button
                      key={key}
                      type="button"
                      aria-label={t("swatch", { name: t(`colors.${key}`), color })}
                      aria-pressed={shown === color}
                      aria-disabled={busy}
                      onClick={(event) => busy || pick(event, color)}
                      // Elegido: una marca dentro, con el texto que contrasta. El anillo es del foco.
                      className="border-foreground/50 flex size-11 items-center justify-center rounded-full border text-base font-bold sm:size-9"
                      style={{ backgroundColor: color, color: inkOn(color) }}
                    >
                      {shown === color ? <span aria-hidden>✓</span> : null}
                    </button>
                  ))}
                </div>
              </fieldset>
              <p className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-muted">{t("preview")}</span>
                <TagChip
                  tag={{ name: preview.name || t("sample"), color: shown ?? "", is_active: true }}
                />
              </p>
            </>
          }
          write={write}
          send={({ name, color }) => {
            const wanted = { name, color: color.toLowerCase() };
            if (!tag) return create.mutate({ orgSlug: slug, data: wanted });
            // Solo lo que cambió respecto a la fila: no pisa lo que otro cambió en lo demás.
            const data = Object.fromEntries(
              Object.entries(wanted).filter(
                ([key, value]) => value !== base.current![key as "name"],
              ),
            );
            if (Object.keys(data).length === 0) {
              show(false); // nada que guardar: se cierra y se dice
              return props.onDone(t("unchanged", { name: base.current!.name }));
            }
            update.mutate({ orgSlug: slug, tagId: tag.id, data });
          }}
          taken={(error) =>
            error.code === "TAG_NAME_TAKEN" ? { field: "name", text: t("nameTaken") } : null
          }
          denied={t("denied")}
          labels={{ submit: t(tag ? "save" : "create"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant={tag ? "ghost" : "accent"}
          className={cn("min-h-11 sm:min-h-9", tag && "border-border border")}
          aria-label={tag ? t("editLabel", { name: tag.name }) : undefined}
          onClick={() => show(true)}
        >
          {t(tag ? "edit" : "open")}
        </Button>
      )}
    </div>
  );
}
