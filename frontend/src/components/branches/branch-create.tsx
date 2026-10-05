"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useBranchesCreate } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";

// Los campos del formulario, en su orden, con el límite que impone la API.
const FIELDS = [
  { name: "code", max: 20 },
  { name: "name", max: 100 },
  { name: "address", max: 255 },
  { name: "district", max: 100 },
  { name: "city", max: 100 },
  { name: "phone", max: 32 },
] as const;
type Field = (typeof FIELDS)[number]["name"];
const REQUIRED = ["code", "name"] as const;
type Required = (typeof REQUIRED)[number];

// Crear una sucursal (F2-47), con `POST /api/v1/o/{slug}/branches/` (F2-44). Nace activa y con
// la zona horaria que pone la API. Quién puede crearla lo decide la API: la pantalla ofrece el
// formulario y explica la respuesta.
export function BranchCreate({ slug, listKey }: { slug: string; listKey: QueryKey }) {
  const t = useTranslations("branches.create");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [missing, setMissing] = useState<readonly Required[]>([]);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const inputs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});
  const submitButton = useRef<HTMLButtonElement>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos envíos seguidos no deben ser dos sucursales.
  const sending = useRef(false);
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
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const error = create.isError && create.error.status !== 401 ? create.error : null;
  const busy = create.isPending || (create.isError && create.error.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  // Por `code`, nunca por el texto de la respuesta. Lo que es de un campo va junto al campo.
  const invalid = error?.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
  function fieldError(field: Field): string | undefined {
    if ((field === "code" || field === "name") && missing.includes(field)) {
      return t(`${field}Required`);
    }
    if (field === "code" && error?.code === "BRANCH_CODE_TAKEN") return t("codeTaken");
    return Object.hasOwn(invalid, field) ? t(`${field}Invalid`) : undefined;
  }
  const firstBad = FIELDS.find((field) => fieldError(field.name))?.name;
  let formError: string | null = null;
  if (error && !firstBad) {
    if (error.code === "PERMISSION_DENIED") formError = t("denied");
    // Un 400 sin campo conocido no es algo que el usuario pueda corregir: es un fallo nuestro.
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va al código. Al cerrar, el control pulsado desaparece: vuelve a
    // «Crear sucursal», salvo que el usuario ya esté en otra parte.
    if (open) inputs.current.code?.focus();
    else if (opened.current && document.activeElement === document.body) trigger.current?.focus();
    opened.current = open;
  }, [open]);
  useEffect(() => {
    // Un error de un campo lleva el foco al primero que lo tiene, si el foco seguía en el
    // botón o en ninguna parte.
    const active = document.activeElement;
    if (firstBad && (active === document.body || active === submitButton.current)) {
      inputs.current[firstBad]?.focus();
    }
  }, [firstBad, error]);

  function show(next: boolean) {
    create.reset();
    setMissing([]);
    if (next) setDone(null);
    setOpen(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending.current) return;
    const form = new FormData(event.currentTarget);
    const value = (field: Field) => String(form.get(field) ?? "").trim();
    const empty = REQUIRED.filter((field) => !value(field));
    if (empty.length > 0) {
      create.reset();
      setMissing(empty);
      inputs.current[empty[0]!]?.focus();
      return;
    }
    sending.current = true;
    create.mutate({
      orgSlug: slug,
      data: {
        code: value("code"),
        name: value("name"),
        address: value("address"),
        district: value("district"),
        city: value("city"),
        phone: value("phone"),
      },
    });
  }

  // Al corregir, el error anterior ya no describe lo escrito.
  // La marca, no `busy`: tras reintentar, la pantalla tarda una tarea en saber que se envía.
  function edited() {
    if (sending.current) return;
    if (missing.length > 0) setMissing([]);
    if (create.isError) create.reset();
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: tras crear reabriría el formulario (y borraría el
      // aviso), y tras un error reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <form
          noValidate
          onSubmit={submit}
          aria-label={t("title")}
          aria-busy={busy}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
        >
          <h2 className="font-medium">{t("title")}</h2>
          {FIELDS.map((field) => (
            <TextField
              key={field.name}
              ref={(input) => {
                inputs.current[field.name] = input;
              }}
              label={t(field.name)}
              name={field.name}
              autoComplete="off"
              inputMode={field.name === "phone" ? "tel" : undefined}
              maxLength={field.max}
              hint={field.name === "code" ? t("codeHint") : undefined}
              error={fieldError(field.name)}
              onInput={edited}
            />
          ))}
          <p className="text-muted text-sm">{t("timezoneNote")}</p>
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
      <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
        {done ? t("done", { name: done }) : ""}
      </p>
    </div>
  );
}
