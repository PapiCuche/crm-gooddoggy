"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useRolesCreate } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";

// Crear un rol propio (F2-35), con `POST /api/v1/o/{slug}/roles/` (F2-29). El rol nace vacío.
// Quién puede crearlo lo decide la API: la pantalla ofrece el formulario y explica la respuesta.
export function RoleCreate({ slug, listKey }: { slug: string; listKey: QueryKey }) {
  const t = useTranslations("roles.create");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [missing, setMissing] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos envíos seguidos no deben ser dos roles.
  const sending = useRef(false);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useRolesCreate({
    mutation: {
      networkMode: "always",
      onSuccess: (role) => {
        setDone(role.name); // el nombre que guardó la API, no el que se escribió
        setOpen(false);
        // El rol nuevo va al final de la lista, que puede no estar cargada entera: se vuelve
        // a pedir lo que hay en pantalla en vez de añadir una fila a mano.
        void queryClient.invalidateQueries({ queryKey: listKey });
      },
    },
  });
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const error = create.isError && create.error.status !== 401 ? create.error : null;
  const busy = create.isPending || (create.isError && create.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  // Por `code`, nunca por el texto de la respuesta. Lo que es del nombre va junto al nombre.
  const invalid = error?.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
  let nameError: string | undefined;
  if (missing) nameError = t("nameRequired");
  else if (error?.code === "ROLE_NAME_TAKEN") nameError = t("nameTaken");
  else if (Object.hasOwn(invalid, "name")) nameError = t("nameInvalid");
  const descriptionError = Object.hasOwn(invalid, "description")
    ? t("descriptionInvalid")
    : undefined;
  let formError: string | null = null;
  if (error && !nameError && !descriptionError) {
    if (error.code === "PERMISSION_DENIED") formError = t("denied");
    // Un 400 sin campo conocido no es algo que el usuario pueda corregir: es un fallo nuestro.
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va al nombre. Al cerrar, el control pulsado desaparece: vuelve a
    // «Crear rol», salvo que el usuario ya esté en otra parte.
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
    create.reset();
    setMissing(false);
    if (next) setDone(null);
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending.current) return;
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim();
    const description = String(form.get("description") ?? "").trim();
    if (!name) {
      create.reset();
      setMissing(true);
      nameField.current?.focus();
      return;
    }
    sending.current = true;
    create.mutate({ orgSlug: slug, data: { name, description } });
  }

  // Al corregir, el error anterior ya no describe lo escrito.
  function edited() {
    if (missing) setMissing(false);
    if (create.isError && !busy) create.reset();
  }

  return (
    <div className="flex flex-col items-start gap-3">
      {open ? (
        <form
          noValidate
          onSubmit={submit}
          aria-label={t("title")}
          aria-busy={busy}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
        >
          <h2 className="font-medium">{t("title")}</h2>
          <TextField
            ref={nameField}
            label={t("name")}
            name="name"
            autoComplete="off"
            maxLength={100}
            hint={t("nameHint")}
            error={nameError}
            onInput={edited}
          />
          <TextField
            label={t("description")}
            name="description"
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
              {t("cancel")}
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
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm" : "sr-only"}>
        {done ? t("done", { name: done }) : ""}
      </p>
    </div>
  );
}
