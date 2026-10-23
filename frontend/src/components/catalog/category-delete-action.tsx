"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useCategoriesDelete } from "@/lib/api/client";
import type { Category } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

import { type Report, rewrite } from "./categories-cache";

// Borrar una categoría (F3-05b) con `DELETE …/categories/{id}/` (F3-04b), tras confirmar, como
// borrar una marca. La API solo la borra sin subcategorías (`CATEGORY_HAS_CHILDREN`) ni productos
// (`CATEGORY_IN_USE`). La fila sale del árbol en cuanto la API lo confirma; como desaparece, el
// resultado lo anuncia el árbol (`onDone`).
export function CategoryDeleteAction({
  slug,
  category,
  listKey,
  onAsk,
  onDone,
}: {
  slug: string;
  category: Category;
  listKey: QueryKey;
  onAsk: () => void; // se abre la confirmación: el aviso anterior del árbol ya no aplica
  onDone: Report;
}) {
  const t = useTranslations("categories.delete");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Lo que se confirma queda fijado al abrir (`null`: cerrado): la pregunta nombra la categoría
  // que el usuario leyó, aunque el árbol cambie debajo.
  const [asking, setAsking] = useState<string | null>(null);
  // Una escritura se envía una vez (el estado de la mutación llega una tarea después).
  const sending = useRef(false);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const remove = useCategoriesDelete({
    mutation: {
      networkMode: "always",
      onSuccess: async (_, { categoryId }) => {
        const focused = here(); // antes de que la fila se vaya: después el foco ya no está en ella
        // La API solo borra sin hijas: si el árbol le enseñaba alguna, otra pestaña las movió.
        let had = false;
        await rewrite(queryClient, listKey, categoryId, (node) => {
          had = node.children.length > 0;
          return null;
        });
        if (had) void queryClient.invalidateQueries({ queryKey: listKey });
        onDone(t("done", { name: asking ?? category.name }), { here: focused });
      },
      onError: (error) => {
        // El árbol podía no enseñar sus hijas: se vuelve a pedir; el error sigue aquí.
        if (error.code === "CATEGORY_HAS_CHILDREN") {
          void queryClient.invalidateQueries({ queryKey: listKey });
        }
        if (error.status !== 404) return;
        onDone(t("stale", { name: asking ?? category.name }), { here: here(), stale: true });
        setAsking(null);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error. Un 404 cierra.
  const failed = remove.isError && remove.error.status !== 401;
  const busy = remove.isPending || (remove.isError && remove.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const open = asking !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va a «Cancelar», que no cambia nada; al cerrar, vuelve a «Borrar».
    if (document.activeElement === document.body) {
      if (open) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: abriría y confirmaría de un solo gesto.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {asking ? (
        <div
          role="group"
          aria-label={t("label", { name: asking })}
          aria-describedby={question}
          className="flex w-full max-w-xl flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { name: asking })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {remove.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    remove.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR" // no hay campos que revisar: es un fallo nuestro
                      : apiErrorKey(remove.error),
                  )}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || setAsking(null)}
            >
              {t("cancel")}
            </Button>
            {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => {
                if (sending.current) return;
                sending.current = true;
                remove.mutate({ orgSlug: slug, categoryId: category.id });
              }}
            >
              {t(busy ? "busy" : "confirm")}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("label", { name: category.name })}
          onClick={() => {
            remove.reset();
            setAsking(category.name);
            onAsk();
          }}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
