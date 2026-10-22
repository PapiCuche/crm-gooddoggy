"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useCategoriesUpdate } from "@/lib/api/client";
import type { PatchedCategoryUpdateRequest } from "@/lib/api/model";
import { cn } from "@/lib/utils";

import { type Report, rewrite } from "./categories-cache";
import type { CategoryNode } from "./categories-tree";
import { MAX_DEPTH, ParentSelect, parentRefused } from "./category-create";

const FIELDS = [{ name: "name", max: 100 }] as const;

// Renombrar y mover una categoría (F3-05b) con `PATCH …/categories/{id}/` (F3-04b): solo se
// envía lo que cambió. Mover lleva toda la rama: no se ofrece dentro de sí misma ni de una
// descendiente, ni donde la rama pasaría del límite de niveles. La API decide igualmente.
export function CategoryEditAction({
  slug,
  category,
  all,
  listKey,
  onAsk,
  onDone,
}: {
  slug: string;
  category: CategoryNode;
  all: CategoryNode[];
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior del árbol ya no aplica
  onDone: Report;
}) {
  const t = useTranslations("categories.edit");
  const fields = useTranslations("categories.create"); // el campo es el del alta
  const parentText = useTranslations("categories.parent");
  const queryClient = useQueryClient();
  // Lo que había al abrir (`null`: cerrado): se compara con eso, no con el árbol que cambie debajo.
  const [from, setFrom] = useState<{ name: string; parent: string } | null>(null);
  const [parent, setParent] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const update = useCategoriesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved, { data }) => {
        const moved = !!data && "parent_id" in data;
        const focused = here(); // antes de que el árbol la cambie de sitio
        await rewrite(queryClient, listKey, saved.id, (node) => ({ ...node, name: saved.name }));
        // Movida, cambian las rutas de toda la rama: sigue «Guardando…» hasta que vuelve el árbol.
        // Si releerlo falla, el árbol de antes sigue en pantalla: se dice.
        let stale = false;
        if (moved) {
          await queryClient.invalidateQueries({ queryKey: listKey });
          stale = queryClient.getQueryState(listKey)?.status === "error";
        }
        setFrom(null);
        const text = moved ? (stale ? "movedStale" : "moved") : "renamed";
        // Movida, la fila se monta en otra lista y el foco no tiene dónde volver: al título.
        onDone(t(text, { name: saved.name }), { here: moved && !stale && focused });
      },
      onError: (error) => {
        if (error.status !== 404) return;
        onDone(t("stale", { name: category.name }), { here: here(), stale: true });
        setFrom(null);
      },
    },
  });

  // La rama (ella y sus descendientes, por `path`) baja entera: cuántos niveles ocupa.
  const branch = all.filter((row) => row.path.startsWith(category.path));
  const levels = Math.max(category.depth, ...branch.map((row) => row.depth)) - category.depth + 1;
  const blocked = (row: CategoryNode) =>
    row.path.startsWith(category.path) || row.depth + levels > MAX_DEPTH;

  const open = from !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Editar», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    update.reset();
    if (next) {
      onAsk();
      setParent(category.parent_id ?? "");
    }
    setFrom(next ? { name: category.name, parent: category.parent_id ?? "" } : null);
  }

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {from ? (
        <FieldsForm
          title={t("title", { name: from.name })}
          heading={
            <p className="text-sm font-medium wrap-anywhere">{t("title", { name: from.name })}</p>
          }
          className="flex w-full max-w-xl flex-col gap-4"
          fields={FIELDS.map((field) => ({
            ...field,
            defaultValue: from.name,
            label: fields("name"),
            invalid: fields("nameInvalid"),
            required: fields("nameRequired"),
          }))}
          note={
            <ParentSelect
              all={all}
              value={parent}
              blocked={blocked}
              busy={update.isPending}
              onChange={setParent}
            />
          }
          write={update}
          send={({ name }) => {
            const data: PatchedCategoryUpdateRequest = {};
            if (name !== from.name) data.name = name;
            if (parent !== from.parent) data.parent_id = parent || null; // `null`: a la raíz
            if (Object.keys(data).length === 0) return show(false); // nada que cambiar
            update.mutate({ orgSlug: slug, categoryId: category.id, data });
          }}
          taken={(error) => {
            if (error.code === "CATEGORY_SLUG_TAKEN") return { text: t("slugTaken") };
            return parentRefused(error) ? { text: parentText("invalid") } : null;
          }}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: fields("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { name: category.name })}
          onClick={() => show(true)}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
