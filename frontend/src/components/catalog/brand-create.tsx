"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useBrandsCreate } from "@/lib/api/client";

const FIELDS = [
  { name: "name", max: 100 },
  { name: "slug", max: 63 },
] as const;

// Crear una marca (F3-02) con `POST /api/v1/o/{slug}/brands/` (F3-01). El slug es opcional: sin
// él, la API lo forma con el nombre. Quién puede crearla lo decide la API.
export function BrandCreate({
  slug,
  listKey,
  onAsk,
}: {
  slug: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
}) {
  const t = useTranslations("brands.create");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useBrandsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: (brand) => {
        setDone(brand.name); // el nombre que guardó la API, no el que se escribió
        setOpen(false);
        // La marca nueva va al final de la lista, que puede no estar cargada entera.
        void queryClient.invalidateQueries({ queryKey: listKey });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Crear marca», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) {
      setDone(null);
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
            invalid: t(`${field.name}Invalid`),
            required: field.name === "name" ? t("nameRequired") : undefined,
            hint: field.name === "slug" ? t("slugHint") : undefined,
          }))}
          write={create}
          // Un slug vacío no se envía: así lo forma la API con el nombre.
          send={({ name, slug: chosen }) =>
            create.mutate({ orgSlug: slug, data: chosen ? { name, slug: chosen } : { name } })
          }
          taken={(error) =>
            error.code === "BRAND_SLUG_TAKEN" ? { field: "slug", text: t("slugTaken") } : null
          }
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
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
        {done ? t("done", { name: done }) : ""}
      </p>
    </div>
  );
}
