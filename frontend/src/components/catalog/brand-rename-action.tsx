"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { type brandsList, useBrandsUpdate } from "@/lib/api/client";
import type { Brand } from "@/lib/api/model";
import { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof brandsList>>>;
const FIELDS = [{ name: "name", max: 100 }] as const;

// Renombrar una marca (F3-02) con `PATCH /api/v1/o/{slug}/brands/{id}/` (F3-01). El slug no
// cambia. La fila se actualiza con lo que devolvió la API; si la marca ya no existe (404), la
// lista avisa y se vuelve a pedir.
export function BrandRenameAction({
  slug,
  brand,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  brand: Brand;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("brands.rename");
  const fields = useTranslations("brands.create"); // el campo es el del alta
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const update = useBrandsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        // Una lectura de la lista en vuelo traería la fila de antes: se cancela y, si era una
        // relectura, se repite después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (saved.id !== brand.id) {
          void queryClient.invalidateQueries({ queryKey: listKey });
          throw new ApiError(500, "INTERNAL_ERROR");
        }
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === saved.id ? { ...row, name: saved.name } : row,
                  ),
                })),
              }
            : data,
        );
        setDone(saved.name);
        setEditing(null);
        if (rereading) void queryClient.invalidateQueries({ queryKey: listKey });
      },
      onError: (error) => {
        if (error.status !== 404) return;
        const active = document.activeElement;
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        onStale(t("stale", { brand: brand.name }), here);
        setEditing(null);
      },
    },
  });

  const open = editing !== null;
  const opened = useRef(false);
  useEffect(() => {
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    update.reset();
    if (next) {
      setDone(null);
      onAsk();
    }
    setEditing(next ? brand.name : null);
  }

  return (
    <div
      ref={root}
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {editing !== null ? (
        <FieldsForm
          title={t("title", { brand: editing })}
          heading={
            <p className="text-sm font-medium wrap-anywhere">{t("title", { brand: editing })}</p>
          }
          className="flex w-full max-w-xl flex-col gap-4"
          fields={FIELDS.map((field) => ({
            ...field,
            defaultValue: editing,
            label: fields("name"),
            invalid: fields("nameInvalid"),
            required: fields("nameRequired"),
          }))}
          write={update}
          send={(data) => update.mutate({ orgSlug: slug, brandId: brand.id, data })}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: fields("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { brand: brand.name })}
          onClick={() => show(true)}
        >
          {t("trigger")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className="sr-only">
        {done ? t("done", { name: done }) : ""}
      </p>
    </div>
  );
}
