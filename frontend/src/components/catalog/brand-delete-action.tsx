"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { type brandsList, useBrandsDelete } from "@/lib/api/client";
import type { Brand } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof brandsList>>>;

// Borrar una marca (F3-03) con `DELETE /api/v1/o/{slug}/brands/{id}/` (F3-01), tras confirmar.
// El borrado es lógico y libera su identificador. La fila sale de la lista en cuanto la API lo
// confirma; como desaparece, el resultado lo anuncia la lista (`onDeleted`). Un 404 (ya borrada
// o de otra organización) avisa en la lista y la vuelve a pedir.
export function BrandDeleteAction({
  slug,
  brand,
  listKey,
  onAsk,
  onStale,
  onDeleted,
}: {
  slug: string;
  brand: Brand;
  listKey: QueryKey;
  onAsk: () => void; // se abre la confirmación: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta tarjeta
  onDeleted: (notice: string, here: boolean) => void;
}) {
  const t = useTranslations("brands.delete");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [asking, setAsking] = useState(false);
  const sending = useRef(false);
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.closest("li")?.contains(active);
  };
  // `networkMode`: sin red falla y se dice; un borrado no queda en cola para después.
  const remove = useBrandsDelete({
    mutation: {
      networkMode: "always",
      onSuccess: async (_, { brandId }) => {
        // Una lectura de la lista en vuelo traería la fila borrada: se cancela y, si era una
        // relectura, se repite después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        const focused = here();
        await queryClient.cancelQueries({ queryKey: listKey });
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.filter((row) => row.id !== brandId),
                })),
              }
            : data,
        );
        onDeleted(t("done", { brand: brand.name }), focused);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        if (error.status !== 404) return;
        onStale(t("stale", { brand: brand.name }), here());
        setAsking(false);
      },
    },
  });
  const failed = remove.isError && remove.error.status !== 401 && remove.error.status !== 404;
  const busy = remove.isPending || (remove.isError && remove.error.status === 401);
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va a «Cancelar» (lo seguro); al cerrar, vuelve a «Borrar».
    if (document.activeElement === document.body || asking) {
      if (asking) cancel.current?.focus();
      else if (opened.current) trigger.current?.focus();
    }
    opened.current = asking;
  }, [asking]);

  function ask() {
    remove.reset();
    setAsking(true);
    onAsk();
  }

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-2", asking && "w-full")}
      // Enter mantenido repite la pulsación: confirmaría sin querer.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {asking ? (
        <div
          role="group"
          aria-label={t("label", { brand: brand.name })}
          aria-describedby={question}
          className="flex flex-col items-start gap-2"
        >
          <p id={question} className="text-sm wrap-anywhere">
            {t("ask", { brand: brand.name, slug: brand.slug })}
          </p>
          {failed ? (
            <p role="alert" className="text-danger text-sm">
              {remove.error.code === "PERMISSION_DENIED"
                ? t("denied")
                : errors(
                    remove.error.code === "VALIDATION_ERROR"
                      ? "INTERNAL_ERROR"
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
              onClick={() => sending.current || setAsking(false)}
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
                remove.mutate({ orgSlug: slug, brandId: brand.id });
              }}
            >
              {busy ? t("busy") : t("confirm")}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { brand: brand.name })}
          onClick={ask}
        >
          {t("trigger")}
        </Button>
      )}
    </div>
  );
}
