"use client";

import {
  type InfiniteData,
  type QueryClient,
  type QueryKey,
  useQueryClient,
} from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useContactNotesDelete, useContactNotesUpdate } from "@/lib/api/client";
import type { Note, PaginatedNoteList, PatchedNoteUpdateRequest } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { NoteForm } from "./note-form";

const ACTION = "border-border min-h-11 border sm:min-h-9";

export type Done = "created" | "updated" | "pinned" | "unpinned" | "deleted";

// Tras una escritura, la lista se relee (la escritura sigue ocupada hasta entonces). Si releerla
// falla, lo escrito entra igual en la caché (con su `version`): la siguiente edición parte de lo
// guardado y no recibe un 409 por su propio cambio. Devuelve si la lista quedó sin releer.
async function settle(client: QueryClient, listKey: QueryKey, change: (rows: Note[]) => Note[]) {
  await client.invalidateQueries({ queryKey: listKey });
  const stale = client.getQueryState(listKey)?.status === "error";
  if (stale) {
    client.setQueryData<InfiniteData<PaginatedNoteList>>(
      listKey,
      (data) =>
        data && {
          ...data,
          pages: data.pages.map((one) => ({ ...one, results: change(one.results) })),
        },
    );
  }
  return stale;
}

// Una nota de la pestaña «Notas» (F4-10): quién la escribió, cuándo, si está fijada y quién la
// editó, si no fue su autor (con `contacts.update` `ORGANIZATION` se editan las ajenas, F4-09).
// El cuerpo es texto, no HTML: React lo escapa y se respetan sus saltos de línea. Editar (solo
// el cuerpo y «fijada», con la `version` leída), fijar y borrar se ofrecen si se puede cambiar
// (`canWrite`: la propia, o la de otro con `ORGANIZATION`); un 404 (se borró, o ya no se puede
// cambiar) lo explica la lista (`onAlert`), que se relee. Un 409 al editar recarga el formulario con lo nuevo, como
// editar un cliente (F4-05): lo escrito no se envía otra vez solo.
export function ContactNote({
  slug,
  contactId,
  listKey,
  note,
  who,
  canWrite,
  onAsk,
  onDone,
  onAlert,
}: {
  slug: string;
  contactId: string;
  listKey: QueryKey;
  note: Note;
  who: (userId: string) => string;
  canWrite: boolean; // esta nota: la propia con `contacts.update`, o cualquiera con `ORGANIZATION`
  onAsk: () => void; // se abre algo: el aviso anterior de la lista ya no aplica
  onDone: (action: Done, stale: boolean, here?: boolean) => void;
  onAlert: (text: string, here: boolean) => void; // `here`: el foco seguía en esta nota
}) {
  const t = useTranslations("contacts.notes");
  const pending = useTranslations("contacts.card")("ownerPending");
  const errors = useTranslations("errors.api");
  const format = useFormatter();
  const client = useQueryClient();
  const meta = useId();
  const question = useId();
  const root = useRef<HTMLDivElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const deleteButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  // Lo que había al abrir (`null`: cerrado): con eso se compara, no con la nota que cambie debajo.
  const [from, setFrom] = useState<Note | null>(null);
  const [round, setRound] = useState(0); // otra vuelta del formulario: lo recargado tras un 409
  const [conflict, setConflict] = useState<"reloaded" | "failed">("reloaded");
  const [asking, setAsking] = useState(false);
  const sending = useRef(false); // fijar y borrar: una escritura por pulsación
  const here = () => {
    const active = document.activeElement;
    return active === document.body || !!root.current?.contains(active);
  };
  const replace = (saved: Note) => (rows: Note[]) =>
    rows.map((row) => (row.id === saved.id ? saved : row));
  function lost() {
    const focused = here();
    setFrom(null);
    setAsking(false);
    onAlert(t("stale"), focused);
  }
  function open(next: Note) {
    setFrom(next);
    setRound((now) => now + 1);
  }

  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const edit = useContactNotesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        const stale = await settle(client, listKey, replace(saved));
        setFrom(null);
        onDone("updated", stale);
      },
      // Sigue «Guardando…» mientras vuelve la lista: el aviso llega con el formulario recargado.
      onError: async (error) => {
        if (error.code === "NOTE_VERSION_CONFLICT") {
          await client.invalidateQueries({ queryKey: listKey });
          if (client.getQueryState(listKey)?.status === "error") return setConflict("failed");
          const fresh = client
            .getQueryData<InfiniteData<PaginatedNoteList>>(listKey)
            ?.pages.flatMap((page) => page.results)
            .find((row) => row.id === note.id);
          if (fresh) {
            setConflict("reloaded");
            return open(fresh);
          }
        } else if (error.status !== 404) return;
        lost(); // un 404, o un 409 de una nota que ya no está
      },
    },
  });
  const pin = useContactNotesUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (saved) => {
        const stale = await settle(client, listKey, replace(saved));
        onDone(saved.is_pinned ? "pinned" : "unpinned", stale);
      },
      onError: (error) => {
        if (error.code === "NOTE_VERSION_CONFLICT") onAlert(t("pinConflict"), false);
        else if (error.status === 404) lost();
      },
    },
  });
  const remove = useContactNotesDelete({
    mutation: {
      networkMode: "always",
      onSuccess: async () => {
        const stale = await settle(client, listKey, (rows) =>
          rows.filter((row) => row.id !== note.id),
        );
        const focused = here();
        setAsking(false);
        onDone("deleted", stale, focused);
      },
      onError: (error) => void (error.status === 404 && lost()),
    },
  });
  // Sin sesión (401), `Providers` lleva al login: sigue ocupado y no se enseña un error.
  const busy = (write: { isPending: boolean; error: ApiError | null }) =>
    write.isPending || write.error?.status === 401;
  // Lo que se dice en la nota; un 404 y un 409 los explica la lista.
  const failure = ({ error }: { error: ApiError | null }) => {
    if (!error || [401, 404].includes(error.status) || error.code === "NOTE_VERSION_CONFLICT") {
      return null;
    }
    return error.code === "PERMISSION_DENIED" ? t("writeDenied") : errors(apiErrorKey(error));
  };
  const pinBusy = busy(pin);
  const removeBusy = busy(remove);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!pinBusy && !removeBusy) sending.current = false;
  });

  const editing = from !== null;
  const opened = useRef({ editing: false, asking: false });
  useEffect(() => {
    // Al abrir la confirmación, el foco va a «Cancelar», que no cambia nada; al cerrar algo, al
    // botón que lo abrió, salvo que el usuario ya esté en otra parte.
    const back = document.activeElement === document.body;
    if (asking) cancelButton.current?.focus();
    else if (opened.current.asking && back) deleteButton.current?.focus();
    if (!editing && opened.current.editing && back) editButton.current?.focus();
    opened.current = { editing, asking };
  }, [editing, asking]);

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
  const pinFailed = failure(pin);
  const removeFailed = failure(remove);

  return (
    <div
      ref={root}
      className="flex flex-col gap-2"
      // Enter mantenido repite la pulsación (en el área de texto es un salto de línea).
      onKeyDown={(event) => {
        const typing = event.target instanceof HTMLTextAreaElement;
        if (event.repeat && event.key === "Enter" && !typing) event.preventDefault();
      }}
    >
      {/* Los espacios separan los datos para el lector de pantalla; el «·» es solo visual. */}
      <p id={meta} className="text-muted flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
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
      {from ? (
        <NoteForm
          key={round}
          focus
          editing={{
            note: from,
            conflict: t(`conflict_${conflict}`),
            onCancel: () => setFrom(null),
          }}
          write={edit}
          send={(body, pinned) => {
            // Solo lo que cambió, con la versión leída: no pisa lo que otro cambió en lo demás.
            const data: PatchedNoteUpdateRequest = { version: from.version };
            if (body !== from.body) data.body = body;
            if (pinned !== from.is_pinned) data.is_pinned = pinned;
            if (Object.keys(data).length === 1) return setFrom(null); // nada que cambiar
            edit.mutate({ orgSlug: slug, contactId, noteId: note.id, data });
          }}
        />
      ) : (
        <p className="wrap-anywhere whitespace-pre-wrap">{note.body}</p>
      )}
      {canWrite && !from && asking ? (
        <div
          role="group"
          aria-label={t("deleteLabel")}
          aria-describedby={question}
          className="flex flex-col items-start gap-2"
        >
          <p id={question} className="text-sm">
            {t("ask")}
          </p>
          {removeFailed ? (
            <p role="alert" className="text-danger text-sm">
              {removeFailed}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancelButton}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              aria-disabled={removeBusy}
              onClick={() => sending.current || setAsking(false)}
            >
              {t("cancel")}
            </Button>
            {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={removeBusy}
              onClick={() => {
                if (sending.current) return;
                sending.current = true;
                remove.mutate({ orgSlug: slug, contactId, noteId: note.id });
              }}
            >
              {removeBusy ? t("deleting") : t("delete")}
            </Button>
          </div>
        </div>
      ) : null}
      {canWrite && !from && !asking ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            ref={editButton}
            variant="ghost"
            className={ACTION}
            aria-describedby={meta}
            onClick={() => {
              edit.reset();
              pin.reset();
              onAsk();
              open(note);
            }}
          >
            {t("edit")}
          </Button>
          <Button
            variant="ghost"
            className={ACTION}
            aria-describedby={meta}
            aria-disabled={pinBusy}
            onClick={() => {
              if (sending.current) return;
              sending.current = true;
              onAsk();
              const data = { is_pinned: !note.is_pinned, version: note.version };
              pin.mutate({ orgSlug: slug, contactId, noteId: note.id, data });
            }}
          >
            {pinBusy ? t("saving") : t(note.is_pinned ? "unpin" : "pinAction")}
          </Button>
          <Button
            ref={deleteButton}
            variant="ghost"
            className={ACTION}
            aria-describedby={meta}
            onClick={() => {
              remove.reset();
              pin.reset();
              onAsk();
              setAsking(true);
            }}
          >
            {t("delete")}
          </Button>
          {pinFailed ? (
            <p role="alert" className="text-danger text-sm">
              {pinFailed}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
