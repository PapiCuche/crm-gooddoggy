"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { type teamsList, useTeamsUpdate } from "@/lib/api/client";
import type { Team } from "@/lib/api/model";
import { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof teamsList>>>;
// Lo que se edita, en su orden, con el límite que impone la API. El identificador no cambia, y
// la forma de asignar no se ofrece: nada la aplica todavía.
const FIELDS = [
  { name: "name", max: 100 },
  { name: "description", max: 255 },
] as const;
type Field = (typeof FIELDS)[number]["name"];
const own = (team: Team) =>
  Object.fromEntries(FIELDS.map((field) => [field.name, team[field.name]])) as Record<
    Field,
    string
  >;

// Cambiar los datos de un equipo (F2-62), con `PATCH …/teams/{id}/` (F2-54). El camino de envío
// es el de `FieldsForm`; aquí está lo propio de editar una fila de la lista, como en «Editar
// sucursal». Quién puede hacerlo lo decide la API.
export function TeamEditAction({
  slug,
  team,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  team: Team;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("teams.edit");
  const fields = useTranslations("teams.create"); // los campos comunes son los del alta
  const queryClient = useQueryClient();
  // Lo que se edita queda fijado al abrir (`null`: cerrado): si la lista cambia debajo, el
  // formulario sigue enseñando lo que el usuario abrió.
  const [editing, setEditing] = useState<Record<Field, string> | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const update = useTeamsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        // Una lectura en vuelo traería los datos de antes y pisaría la tarjeta: se cancela. Si
        // era la lista entera (no «Cargar más»), se vuelve a pedir después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (saved.id !== team.id) {
          // No es el equipo que se pidió: no se da por guardado, y se explica como un fallo
          // nuestro.
          void queryClient.invalidateQueries({ queryKey: listKey });
          throw new ApiError(500, "INTERNAL_ERROR");
        }
        // La tarjeta enseña lo que guardó la API en estos campos; lo demás de la fila pudo
        // cambiarlo otra escritura de esta pantalla después de esa respuesta, y no se pisa.
        const fresh = own(saved);
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === saved.id ? { ...row, ...fresh } : row,
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
        // El equipo ya no está al alcance (o la organización, o la membresía): lo explica la
        // lista, que se vuelve a pedir.
        if (error.status !== 404) return;
        const active = document.activeElement;
        // La tarjeta entera desaparece: también si el foco estaba en ella o en otra acción suya.
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        onStale(t("stale", { team: team.name }), here);
        setEditing(null);
      },
    },
  });

  const open = editing !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el control pulsado desaparece: el foco vuelve a «Editar», salvo que el
    // usuario ya esté en otra parte.
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
    setEditing(next ? own(team) : null);
  }

  return (
    <div
      ref={root}
      // Abierta ocupa su fila: una acción vecina de la tarjeta pasa a otra línea.
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {editing ? (
        <FieldsForm
          title={t("title", { team: editing.name })}
          heading={
            <p className="text-sm font-medium wrap-anywhere">
              {t("title", { team: editing.name })}
            </p>
          }
          className="flex w-full max-w-xl flex-col gap-4"
          fields={FIELDS.map((field) => ({
            ...field,
            defaultValue: editing[field.name],
            label: fields(field.name),
            invalid: fields(`${field.name}Invalid`),
            required: field.name === "name" ? fields("nameRequired") : undefined,
          }))}
          write={update}
          send={(data) => update.mutate({ orgSlug: slug, teamId: team.id, data })}
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: fields("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          // Con el identificador: el nombre de un equipo puede repetirse en la organización.
          aria-label={t("triggerLabel", { team: team.name, slug: team.slug })}
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
