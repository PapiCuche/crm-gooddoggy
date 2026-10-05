"use client";

import { type InfiniteData, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { type rolesList, useRolesUpdate } from "@/lib/api/client";
import type { Role } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Pages = InfiniteData<Awaited<ReturnType<typeof rolesList>>>;

// Cambiar el nombre y la descripción de un rol (F2-39), con `PATCH …/roles/{id}/` (F2-38).
// Hermano de «Crear rol»: mismos campos, mismas reglas de envío, foco y errores. Quién puede
// hacerlo lo decide la API (cubrir el rol, nombres que no se confunden).
export function RoleEditAction({
  slug,
  role,
  listKey,
  onAsk,
  onStale,
}: {
  slug: string;
  role: Role;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el aviso anterior de la lista ya no aplica
  onStale: (notice: string, here: boolean) => void; // `here`: el foco seguía en esta acción
}) {
  const t = useTranslations("roles.edit");
  const fields = useTranslations("roles.create"); // los campos son los del alta
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  // Lo que se edita queda fijado al abrir (`null`: cerrado): si la lista cambia debajo, el
  // formulario sigue enseñando lo que el usuario abrió.
  const [editing, setEditing] = useState<{ name: string; description: string } | null>(null);
  const [missing, setMissing] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  // Una escritura se envía una vez (el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación).
  const sending = useRef(false);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const update = useRolesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        // Una lectura en vuelo traería el nombre de antes y pisaría la tarjeta: se cancela. Si
        // era la lista entera (no «Cargar más»), se vuelve a pedir después.
        const read = queryClient.getQueryState(listKey);
        const rereading = !!read && read.fetchStatus !== "idle" && !read.fetchMeta?.fetchMore;
        await queryClient.cancelQueries({ queryKey: listKey });
        if (saved.id !== role.id) {
          // No es el rol que se pidió: no se da por guardado, y se explica como un fallo nuestro.
          void queryClient.invalidateQueries({ queryKey: listKey });
          throw new ApiError(500, "INTERNAL_ERROR");
        }
        // La tarjeta enseña el nombre y la descripción que guardó la API; lo demás de la fila pudo
        // cambiarlo otra escritura de esta pantalla después de esa respuesta, y no se pisa.
        const own = { name: saved.name, description: saved.description };
        queryClient.setQueryData<Pages>(listKey, (data) =>
          data
            ? {
                ...data,
                pages: data.pages.map((page) => ({
                  ...page,
                  results: page.results.map((row) =>
                    row.id === saved.id ? { ...row, ...own } : row,
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
        // El rol ya no existe: lo explica la lista, que se vuelve a pedir.
        if (error.status !== 404) return;
        const active = document.activeElement;
        // La tarjeta entera desaparece: también si el foco estaba en «Permisos», a su lado.
        const here = active === document.body || !!root.current?.closest("li")?.contains(active);
        onStale(t("stale", { role: role.name }), here);
        setEditing(null);
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const gone = update.isError && (update.error.status === 401 || update.error.status === 404);
  const error = update.isError && !gone ? update.error : null;
  const busy = update.isPending || (update.isError && update.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  // Por `code`, nunca por el texto de la respuesta. Lo que es del nombre va junto al nombre.
  const invalid = error?.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
  let nameError: string | undefined;
  if (missing) nameError = fields("nameRequired");
  else if (error?.code === "ROLE_NAME_TAKEN") nameError = fields("nameTaken");
  else if (Object.hasOwn(invalid, "name")) nameError = fields("nameInvalid");
  const descriptionError = Object.hasOwn(invalid, "description")
    ? fields("descriptionInvalid")
    : undefined;
  let formError: string | null = null;
  if (error && !nameError && !descriptionError) {
    if (error.code === "PERMISSION_DENIED") formError = t("denied");
    // Un 400 sin campo conocido no es algo que el usuario pueda corregir: es un fallo nuestro.
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  const open = editing !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va al nombre. Al cerrar, el control pulsado desaparece: vuelve a
    // «Editar», salvo que el usuario ya esté en otra parte.
    if (open) nameField.current?.focus();
    else if (opened.current && document.activeElement === document.body) trigger.current?.focus();
    opened.current = open;
  }, [open]);
  useEffect(() => {
    // Un error del nombre lleva el foco al nombre, si seguía en el botón o en ninguna parte.
    const active = document.activeElement;
    if (nameError && (active === document.body || active === submitButton.current)) {
      nameField.current?.focus();
    }
  }, [nameError]);

  function show(next: boolean) {
    update.reset();
    setMissing(false);
    if (next) {
      setDone(null);
      onAsk();
    }
    setEditing(next ? { name: role.name, description: role.description } : null);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending.current) return;
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const description = String(form.get("description") ?? "").trim();
    if (!name) {
      update.reset();
      setMissing(true);
      nameField.current?.focus();
      return;
    }
    sending.current = true;
    update.mutate({ orgSlug: slug, roleId: role.id, data: { name, description } });
  }

  // Al corregir, el error anterior ya no describe lo escrito. La marca, no `busy`: tras
  // reintentar, la pantalla tarda una tarea en saber que se envía.
  function edited() {
    if (sending.current) return;
    if (missing) setMissing(false);
    if (update.isError) update.reset();
  }

  return (
    <div
      ref={root}
      // Abierta ocupa su fila: la acción vecina de la tarjeta pasa a otra línea.
      className={cn("flex flex-col items-start gap-2", open && "w-full")}
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {editing ? (
        <form
          noValidate
          onSubmit={submit}
          aria-label={t("title", { role: editing.name })}
          aria-busy={busy}
          className="flex w-full max-w-xl flex-col gap-4"
        >
          <p className="text-sm font-medium wrap-anywhere">{t("title", { role: editing.name })}</p>
          <TextField
            ref={nameField}
            label={fields("name")}
            name="name"
            defaultValue={editing.name}
            autoComplete="off"
            maxLength={100}
            hint={fields("nameHint")}
            error={nameError}
            onInput={edited}
          />
          <TextField
            label={fields("description")}
            name="description"
            defaultValue={editing.description}
            autoComplete="off"
            maxLength={255}
            error={descriptionError}
            onInput={edited}
          />
          {formError ? (
            <p role="alert" className="text-danger text-sm">
              {formError}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            {/* `type`: dentro de un formulario, un botón sin tipo lo envía. */}
            <Button
              type="button"
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => sending.current || show(false)}
            >
              {fields("cancel")}
            </Button>
            {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
            <Button
              ref={submitButton}
              type="submit"
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
            >
              {t(busy ? "busy" : "submit")}
            </Button>
          </div>
        </form>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          aria-label={t("triggerLabel", { role: role.name })}
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
