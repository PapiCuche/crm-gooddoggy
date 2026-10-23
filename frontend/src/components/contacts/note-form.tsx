"use client";

import { useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Write = { isPending: boolean; isError: boolean; error: ApiError | null; reset: () => void };

// Añadir una nota (F4-10): el cuerpo en un área de texto y «Fijar la nota». Como `FieldsForm`:
// un envío por pulsación (la marca se suelta cuando la pantalla ya enseña el resultado), errores
// por `code` y nunca con el texto de la respuesta (que no repite el cuerpo); sin sesión (401)
// sigue ocupado: `Providers` lleva al login. El límite (5000 caracteres tras NFC) solo se ayuda:
// lo cuenta la API, y `maxLength` contaría unidades UTF-16 y cortaría lo que ella sí acepta.
export function NoteForm({
  write,
  send,
  focus,
}: {
  write: Write;
  send: (body: string, pinned: boolean) => void;
  focus: boolean; // al montarse, el foco al área de texto si no está en otra parte
}) {
  const t = useTranslations("contacts.notes");
  const errors = useTranslations("errors.api");
  const id = useId();
  const [body, setBody] = useState("");
  const [pinned, setPinned] = useState(false);
  const [empty, setEmpty] = useState(false); // se pulsó sin nada escrito
  const area = useRef<HTMLTextAreaElement>(null);
  const sending = useRef(false);
  const sentWith = useRef<Write | null>(null);
  const busy = write.isPending || (write.isError && write.error?.status === 401);
  useLayoutEffect(() => {
    if (!busy && write !== sentWith.current) sending.current = false;
  });
  const error = !empty && write.isError && write.error?.status !== 401 ? write.error : null;
  const bad = error?.code === "VALIDATION_ERROR" && Object.hasOwn(error.fields ?? {}, "body");
  const fieldError = empty ? t("required") : bad ? t("invalid") : null;
  let formError: string | null = null;
  if (error && !bad) {
    if (error.status === 404) formError = t("contactGone");
    else if (error.code === "PERMISSION_DENIED") formError = t("writeDenied");
    else
      formError = errors(error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error));
  }

  // Al corregir, el aviso anterior ya no describe lo escrito (mientras se envía, no se toca).
  function edited() {
    if (sending.current) return;
    setEmpty(false);
    if (write.isError) write.reset();
  }

  useEffect(() => {
    // Tras añadir una nota (el formulario se monta de nuevo, vacío): al área de texto.
    if (focus && document.activeElement === document.body) area.current?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- solo al montar
  }, []);

  return (
    <form
      noValidate
      aria-label={t("new")}
      aria-busy={busy}
      className="flex w-full flex-col gap-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (sending.current) return;
        setEmpty(!body.trim());
        if (!body.trim()) {
          write.reset();
          area.current?.focus();
          return;
        }
        sending.current = true;
        sentWith.current = write;
        send(body.trim(), pinned);
      }}
    >
      <h2 className="text-lg font-semibold">{t("new")}</h2>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={id} className="text-sm font-medium">
          {t("body")}
        </label>
        <textarea
          ref={area}
          id={id}
          name="body"
          rows={4}
          value={body}
          aria-invalid={fieldError ? true : undefined}
          aria-describedby={`${id}-note`}
          readOnly={busy}
          onChange={(event) => {
            setBody(event.target.value);
            edited();
          }}
          className={cn(
            "bg-surface border-foreground/50 min-h-28 resize-y rounded-[10px] border px-3 py-2.5 text-[16px] leading-relaxed",
            "focus-visible:border-foreground aria-invalid:border-danger",
          )}
        />
        <p
          id={`${id}-note`}
          role={fieldError ? "alert" : undefined}
          className={cn("text-sm", fieldError ? "text-danger" : "text-muted")}
        >
          {fieldError ?? t("hint")}
        </p>
      </div>
      <label className="flex min-h-11 items-center gap-2 self-start text-sm sm:min-h-9">
        <input
          type="checkbox"
          checked={pinned}
          aria-disabled={busy}
          onChange={(event) => {
            if (busy) return;
            setPinned(event.target.checked);
            edited();
          }}
          className="accent-foreground size-4"
        />
        {t("pin")}
      </label>
      {formError ? (
        <p role="alert" className="text-danger text-sm">
          {formError}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
        <Button
          type="submit"
          variant="primary"
          className="min-h-11 sm:min-h-9"
          aria-disabled={busy}
        >
          {t(busy ? "adding" : "add")}
        </Button>
      </div>
    </form>
  );
}
