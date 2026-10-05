"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useBranchesCreate } from "@/lib/api/client";

// Los campos del formulario, en su orden, con el límite que impone la API.
const FIELDS = [
  { name: "code", max: 20 },
  { name: "name", max: 100 },
  { name: "address", max: 255 },
  { name: "district", max: 100 },
  { name: "city", max: 100 },
  { name: "phone", max: 32 },
] as const;

// Crear una sucursal (F2-47), con `POST /api/v1/o/{slug}/branches/` (F2-44). Nace activa y con
// la zona horaria que pone la API. Quién puede crearla lo decide la API: la pantalla ofrece el
// formulario y explica la respuesta. El camino de envío es el de `FieldsForm`.
export function BranchCreate({
  slug,
  listKey,
  onAsk,
}: {
  slug: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
}) {
  const t = useTranslations("branches.create");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useBranchesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: (branch) => {
        setDone(branch.name); // el nombre que guardó la API, no el que se escribió
        setOpen(false);
        // La sucursal nueva va al final de la lista, que puede no estar cargada entera: se
        // vuelve a pedir lo que hay en pantalla en vez de añadir una fila a mano.
        void queryClient.invalidateQueries({ queryKey: listKey });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el control pulsado desaparece: el foco vuelve a «Crear sucursal», salvo que
    // el usuario ya esté en otra parte.
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
      // Enter mantenido repite la pulsación: tras crear reabriría el formulario (y borraría el
      // aviso), y tras un error reenviaría sin parar.
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
            required:
              field.name === "code" || field.name === "name"
                ? t(`${field.name}Required`)
                : undefined,
            hint: field.name === "code" ? t("codeHint") : undefined,
            inputMode: field.name === "phone" ? ("tel" as const) : undefined,
          }))}
          write={create}
          send={(data) => create.mutate({ orgSlug: slug, data })}
          taken={(error) =>
            error.code === "BRANCH_CODE_TAKEN" ? { field: "code", text: t("codeTaken") } : null
          }
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          note={<p className="text-muted text-sm">{t("timezoneNote")}</p>}
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
