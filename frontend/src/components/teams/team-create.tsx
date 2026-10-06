"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useTeamsCreate } from "@/lib/api/client";

// Los campos del formulario, en su orden, con el límite que impone la API.
const FIELDS = [
  { name: "slug", max: 50 },
  { name: "name", max: 100 },
  { name: "description", max: 255 },
] as const;

// Crear un equipo (F2-61), con `POST /api/v1/o/{slug}/teams/` (F2-53). Nace activo, sin
// integrantes y con la forma de asignar que pone la API. Quién puede crearlo lo decide la API:
// la pantalla ofrece el formulario y explica la respuesta. El camino de envío es el de
// `FieldsForm`.
export function TeamCreate({ slug, listKey }: { slug: string; listKey: QueryKey }) {
  const t = useTranslations("teams.create");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useTeamsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: (team) => {
        setDone(team.name); // el nombre que guardó la API, no el que se escribió
        setOpen(false);
        // El equipo nuevo va al final de la lista, que puede no estar cargada entera: se
        // vuelve a pedir lo que hay en pantalla en vez de añadir una fila a mano.
        void queryClient.invalidateQueries({ queryKey: listKey });
      },
    },
  });

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el control pulsado desaparece: el foco vuelve a «Crear equipo», salvo que el
    // usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) setDone(null);
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
            required: field.name === "description" ? undefined : t(`${field.name}Required`),
            hint: field.name === "slug" ? t("slugHint") : undefined,
          }))}
          write={create}
          send={(data) => create.mutate({ orgSlug: slug, data })}
          taken={(error) =>
            error.code === "TEAM_SLUG_TAKEN" ? { field: "slug", text: t("slugTaken") } : null
          }
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          note={<p className="text-muted text-sm">{t("strategyNote")}</p>}
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
