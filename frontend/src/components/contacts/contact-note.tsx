"use client";

import { useFormatter, useTranslations } from "next-intl";

import type { Note } from "@/lib/api/model";

// Una nota de la pestaña «Notas» (F4-10): quién la escribió, cuándo, si está fijada y quién la
// editó, si no fue su autor (con `contacts.update` `ORGANIZATION` se editan las ajenas, F4-09).
// El cuerpo es texto, no HTML: React lo escapa y se respetan sus saltos de línea.
export function ContactNote({ note, who }: { note: Note; who: (userId: string) => string }) {
  const t = useTranslations("contacts.notes");
  const pending = useTranslations("contacts.card")("ownerPending");
  const format = useFormatter();
  // Mientras llega el directorio: «…» a la vista y «Cargando» para el lector de pantalla.
  const name = (text: string) =>
    text === pending ? (
      <>
        <span aria-hidden>…</span>
        <span className="sr-only">{text}</span>
      </>
    ) : (
      text
    );
  const editor =
    note.updated_by_user_id && note.updated_by_user_id !== note.author_user_id
      ? who(note.updated_by_user_id)
      : null;

  return (
    <div className="flex flex-col gap-2">
      {/* Los espacios separan los datos para el lector de pantalla; el «·» es solo visual. */}
      <p className="text-muted flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        <span className="text-foreground font-medium wrap-anywhere">
          {name(who(note.author_user_id))}
        </span>{" "}
        <span aria-hidden>·</span>{" "}
        <time dateTime={note.created_at}>
          {format.dateTime(new Date(note.created_at), { dateStyle: "medium", timeStyle: "short" })}
        </time>
        {editor && editor !== pending ? (
          <>
            {" "}
            <span className="wrap-anywhere">{t("editedBy", { name: editor })}</span>
          </>
        ) : null}
        {note.is_pinned ? (
          <>
            {" "}
            <span className="bg-accent text-accent-foreground rounded-full px-2 py-0.5 text-xs font-medium">
              {t("pinned")}
            </span>
          </>
        ) : null}
      </p>
      <p className="wrap-anywhere whitespace-pre-wrap">{note.body}</p>
    </div>
  );
}
