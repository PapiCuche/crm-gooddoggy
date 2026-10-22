"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useCategoriesCreate } from "@/lib/api/client";
import type { ApiError } from "@/lib/http";

import type { CategoryNode } from "./categories-tree";

const MAX_DEPTH = 5; // los niveles que admite la API (F3-04)
const FIELDS = [
  { name: "name", max: 100 },
  { name: "slug", max: 63 },
] as const;

// Dónde va una categoría: en la raíz o dentro de otra. Las opciones siguen el árbol, con sangría;
// las que no se pueden elegir quedan visibles y desactivadas. La API decide igualmente.
function ParentSelect({
  all,
  value,
  blocked,
  busy,
  onChange,
}: {
  all: CategoryNode[];
  value: string; // el id; vacío: la raíz
  blocked: (row: CategoryNode) => boolean;
  busy: boolean;
  onChange: (id: string) => void;
}) {
  const t = useTranslations("categories.parent");
  const id = useId();
  const names = new Map(all.map((row) => [row.id, row.name]));
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium">
        {t("label")}
      </label>
      {/* 16 px en el control: por debajo, Safari en iOS amplía la página al enfocarlo. */}
      <select
        id={id}
        value={value}
        aria-disabled={busy}
        aria-describedby={`${id}-hint`}
        className="bg-surface border-foreground/50 focus-visible:border-foreground h-11 w-full rounded-[10px] border px-3 text-[16px]"
        onChange={(event) => busy || onChange(event.target.value)} // mientras se envía, no cambia
      >
        <option value="">{t("root")}</option>
        {all.map((row) => {
          // La sangría no se oye: con la madre en el texto, dos «Accesorios» se distinguen.
          const parent = row.parent_id ? names.get(row.parent_id) : undefined;
          const text = parent ? t("inside", { name: row.name, parent }) : row.name;
          return (
            <option key={row.id} value={row.id} disabled={blocked(row)}>
              {"   ".repeat(row.depth - 1) + text}
            </option>
          );
        })}
      </select>
      <p id={`${id}-hint`} className="text-muted text-sm">
        {t("hint", { max: MAX_DEPTH })}
      </p>
    </div>
  );
}

// Un `parent_id` que la API no acepta (ya no existe, o la rama pasaría del límite).
const parentRefused = (error: ApiError) =>
  error.code === "VALIDATION_ERROR" && !!error.fields && Object.hasOwn(error.fields, "parent_id");

// Crear una categoría (F3-05) con `POST /api/v1/o/{slug}/categories/` (F3-04), en la raíz o
// dentro de otra. El slug es opcional: sin él, la API lo forma con el nombre.
export function CategoryCreate({
  slug,
  all,
  listKey,
  onAsk,
  onDone,
}: {
  slug: string;
  all: CategoryNode[];
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el anuncio anterior ya no aplica
  onDone: (text: string) => void;
}) {
  const t = useTranslations("categories.create");
  const parentText = useTranslations("categories.parent");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [parent, setParent] = useState("");
  // El último envío iba sin slug: un 400 o un 409 del slug habla del que la API formó.
  const [formed, setFormed] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useCategoriesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: async (category) => {
        // Sigue «Creando…» hasta que el árbol trae la categoría nueva en su sitio. Si releerlo
        // falla, la lista sigue enseñando el árbol de antes: se dice, para no dar por hecho.
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        setOpen(false);
        onDone(t(stale ? "doneStale" : "done", { name: category.name }));
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Crear categoría», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) {
      setParent("");
      onAsk();
    }
    setOpen(next);
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={FIELDS.map((field) => ({
            ...field,
            label: t(field.name),
            invalid: field.name === "slug" && formed ? t("slugNeeded") : t(`${field.name}Invalid`),
            required: field.name === "name" ? t("nameRequired") : undefined,
            hint: field.name === "slug" ? t("slugHint") : undefined,
            literal: field.name === "slug",
          }))}
          note={
            <ParentSelect
              all={all}
              value={parent}
              blocked={(row) => row.depth >= MAX_DEPTH}
              busy={create.isPending}
              onChange={setParent}
            />
          }
          write={create}
          // Un slug vacío no se envía (lo forma la API), ni un `parent_id` para la raíz.
          send={({ name, slug: chosen }) => {
            setFormed(!chosen);
            create.mutate({
              orgSlug: slug,
              data: {
                name,
                ...(chosen ? { slug: chosen } : {}),
                ...(parent ? { parent_id: parent } : {}),
              },
            });
          }}
          taken={(error) => {
            if (error.code === "CATEGORY_SLUG_TAKEN") {
              return { field: "slug", text: t(formed ? "slugTakenFormed" : "slugTaken") };
            }
            return parentRefused(error) ? { text: parentText("invalid") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
