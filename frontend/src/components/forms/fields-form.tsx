"use client";

import { useTranslations } from "next-intl";
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

// Un campo de texto del formulario. Los textos llegan ya resueltos: los pone la pantalla.
export type FormField<Name extends string> = {
  name: Name;
  label: string;
  max: number; // el límite de la API
  invalid: string; // lo que se dice si la API no acepta el campo
  required?: string; // lo que se dice si falta; sin él, el campo es opcional
  hint?: string;
  inputMode?: "tel";
  defaultValue?: string; // lo que hay al abrir (un formulario de edición)
  list?: string; // el `id` de un `<datalist>` de sugerencias
};
// Lo que el formulario necesita saber de la escritura: el resultado de `useMutation` en el
// render de la pantalla (un objeto nuevo cada vez que ella se pinta).
type Write = { isPending: boolean; isError: boolean; error: ApiError | null; reset: () => void };

// Los campos de texto de una escritura (F2-48), con su camino de envío: un envío por pulsación,
// errores por `code` junto a su campo, y el foco donde hace falta. Lo comparten los formularios
// de gestión. La pantalla aporta la escritura, qué hacer con lo escrito y sus textos; abrir y
// cerrar, anunciar el resultado y actualizar la lista siguen siendo suyos.
export function FieldsForm<Name extends string>({
  title,
  heading,
  fields,
  write,
  send,
  taken,
  denied,
  labels,
  note,
  onCancel,
  className,
}: {
  title: string; // el nombre accesible del formulario
  heading: ReactNode;
  fields: readonly FormField<Name>[];
  write: Write;
  // Recibe lo escrito, sin espacios exteriores, e inicia la escritura antes de volver.
  send: (values: Record<Name, string>) => void;
  taken?: (error: ApiError) => { field: Name; text: string } | null; // un error propio de un campo
  denied: string;
  labels: { submit: string; busy: string; cancel: string };
  note?: ReactNode; // bajo los campos
  onCancel: () => void;
  className?: string;
}) {
  const errors = useTranslations("errors.api");
  const [missing, setMissing] = useState<readonly Name[]>([]);
  // La escritura cuyo error ya se retiró aquí: la pantalla tarda una tarea en traerla vacía.
  const [dropped, setDropped] = useState<Write | null>(null);
  const inputs = useRef<Partial<Record<Name, HTMLInputElement | null>>>({});
  const submitButton = useRef<HTMLButtonElement>(null);
  // Una escritura se envía una vez: el estado de la mutación llega a la pantalla una tarea
  // después de la pulsación, y dos envíos seguidos no deben ser dos escrituras.
  const sending = useRef(false);
  const sentWith = useRef<Write | null>(null); // la escritura tal como era al enviar
  // Sin sesión (401), `Providers` lleva al login: aquí no se enseña un error.
  const failed = write.isError && write !== dropped;
  const error = failed && write.error?.status !== 401 ? write.error : null;
  const busy = write.isPending || (write.isError && write.error?.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  // Un render solo de este formulario trae la escritura de antes de enviar, que aún no dice
  // «enviando»: ese no la suelta.
  useLayoutEffect(() => {
    if (!busy && write !== sentWith.current) sending.current = false;
  });

  // Por `code`, nunca por el texto de la respuesta. Lo que es de un campo va junto al campo.
  const invalid = error?.code === "VALIDATION_ERROR" ? (error.fields ?? {}) : {};
  const own = error ? (taken?.(error) ?? null) : null;
  function fieldError(field: FormField<Name>): string | undefined {
    if (missing.includes(field.name)) return field.required;
    if (own?.field === field.name) return own.text;
    return Object.hasOwn(invalid, field.name) ? field.invalid : undefined;
  }
  const firstBad = fields.find((field) => fieldError(field))?.name;
  let formError: string | null = null;
  if (error && !firstBad) {
    if (error.code === "PERMISSION_DENIED") formError = denied;
    // Un 400 sin campo conocido no es algo que el usuario pueda corregir: es un fallo nuestro.
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  useEffect(() => {
    inputs.current[fields[0]!.name]?.focus(); // al abrir, el foco va al primer campo
    // eslint-disable-next-line react-hooks/exhaustive-deps -- solo al montar
  }, []);
  useEffect(() => {
    // Un error de un campo lleva el foco al primero que lo tiene, si el foco seguía en el
    // botón o en ninguna parte.
    const active = document.activeElement;
    if (firstBad && (active === document.body || active === submitButton.current)) {
      inputs.current[firstBad]?.focus();
    }
  }, [firstBad, error]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending.current) return;
    const form = new FormData(event.currentTarget);
    const values = Object.fromEntries(
      fields.map((field) => [field.name, String(form.get(field.name) ?? "").trim()]),
    ) as Record<Name, string>;
    const empty = fields.filter((field) => field.required && !values[field.name]);
    if (empty.length > 0) {
      write.reset();
      setDropped(write);
      setMissing(empty.map((field) => field.name));
      inputs.current[empty[0]!.name]?.focus();
      return;
    }
    if (missing.length > 0) setMissing([]); // un aviso anterior no tapa la respuesta de la API
    sending.current = true;
    sentWith.current = write;
    send(values);
  }

  // Al corregir, el error anterior ya no describe lo escrito.
  // La marca, no `busy`: tras reintentar, la pantalla tarda una tarea en saber que se envía.
  function edited() {
    if (sending.current) return;
    if (missing.length > 0) setMissing([]);
    if (write.isError) write.reset();
  }

  return (
    <form noValidate onSubmit={submit} aria-label={title} aria-busy={busy} className={className}>
      {heading}
      {fields.map((field) => (
        <TextField
          key={field.name}
          ref={(input) => {
            inputs.current[field.name] = input;
          }}
          label={field.label}
          name={field.name}
          autoComplete="off"
          inputMode={field.inputMode}
          defaultValue={field.defaultValue}
          list={field.list}
          maxLength={field.max}
          hint={field.hint}
          error={fieldError(field)}
          onInput={edited}
        />
      ))}
      {note}
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
          onClick={() => sending.current || onCancel()}
        >
          {labels.cancel}
        </Button>
        {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
        <Button
          ref={submitButton}
          type="submit"
          variant="primary"
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
        >
          {busy ? labels.busy : labels.submit}
        </Button>
      </div>
    </form>
  );
}
