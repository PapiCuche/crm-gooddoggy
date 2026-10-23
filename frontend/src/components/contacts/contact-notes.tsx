"use client";

import { type InfiniteData, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { notFound } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import {
  contactNotesList,
  getContactNotesListQueryKey,
  useContactNotesCreate,
} from "@/lib/api/client";
import type { PaginatedNoteList } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { ContactNote } from "./contact-note";
import { useContactNames } from "./contact-names";
import { NoteForm } from "./note-form";

const ROW = "border-border bg-surface rounded-lg border p-4 focus-visible:outline-none";

// La pestaña «Notas» de la ficha (F4-10), con `…/contacts/{id}/notes/` (F4-09): de la más
// reciente a la más antigua, por cursor (ADR-016); las fijadas se marcan, no se adelantan. Un
// 404 de la lista es el del contacto: la página de «no encontrada», como la ficha. Carga, error
// con «Reintentar», 403 y 401 como en `CursorList` (que no se usa: pinta su propia página).
// Con `contacts.update` (cualquier alcance) se añaden; qué contacto admite notas lo decide la API.
export function ContactNotes({ contactId }: { contactId: string }) {
  const t = useTranslations("contacts.notes");
  const errors = useTranslations("errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const canWrite = permissions.some((grant) => grant.code === "contacts.update");
  const { who } = useContactNames("contacts.card");
  const client = useQueryClient();
  const listKey = [...getContactNotesListQueryKey(slug, contactId), "pages"];
  const list = useInfiniteQuery<PaginatedNoteList, ApiError>({
    queryKey: listKey,
    queryFn: ({ pageParam, signal }) =>
      contactNotesList(slug, contactId, pageParam ? { cursor: pageParam as string } : undefined, {
        signal,
      }),
    initialPageParam: null,
    getNextPageParam: (last) => last.next,
    gcTime: 0, // como la ficha: al salir no queda en memoria
  });
  const heading = useRef<HTMLHeadingElement>(null);
  const retryButton = useRef<HTMLButtonElement>(null);
  const moreButton = useRef<HTMLButtonElement>(null);
  const fresh = useRef<HTMLLIElement>(null); // la primera nota de la última página cargada
  const [retrying, setRetrying] = useState(false);
  const [said, setSaid] = useState(""); // lo que respondió la última escritura
  const [round, setRound] = useState(0); // otra vuelta del formulario: vacío tras añadir
  const [more, setMore] = useState<"idle" | "busy" | "arrived">("idle");
  const status = list.error?.status;
  const [denied, setDenied] = useState(false);
  if (status === 403 && !denied) setDenied(true);
  if (denied && list.isSuccess) setDenied(false);
  if (status === 404) notFound();

  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useContactNotesCreate({
    mutation: {
      networkMode: "always",
      onMutate: () => setSaid(""),
      onSuccess: async (note) => {
        // Sigue «Añadiendo…» hasta que vuelve la lista, con la nota nueva la primera. Si no
        // vuelve, la nota entra igual en la caché, la primera (si la lista llegó a cargarse).
        await client.invalidateQueries({ queryKey: listKey });
        const stale = client.getQueryState(listKey)?.status === "error";
        if (stale) {
          client.setQueryData<InfiniteData<PaginatedNoteList>>(listKey, (data) => {
            if (!data) return data; // nunca llegó: no hay dónde ponerla, y no es un error
            const [first, ...rest] = data.pages;
            return { ...data, pages: [{ ...first!, results: [note, ...first!.results] }, ...rest] };
          });
        }
        setRound((now) => now + 1);
        setSaid(t(stale ? "doneStale" : "done"));
      },
      // Fuera de su alcance de edición, o el contacto se borró: lo dice el formulario, y releer
      // la lista lleva a «no encontrada» si el contacto ya no está.
      onError: (error) => void (error.status === 404 && list.refetch()),
    },
  });

  const pages = list.data?.pages ?? [];
  const notes = pages.flatMap((page) => page.results);
  const firstOfLast = pages.length > 1 ? pages.at(-1)?.results[0]?.id : undefined;
  useEffect(() => {
    // «Cargar más» desaparece en la última página: el foco sigue en lo que llegó, si no se fue.
    const at = document.activeElement;
    if (more === "arrived" && (at === document.body || at === moreButton.current)) {
      fresh.current?.focus();
    }
  }, [more]);

  async function retry() {
    setRetrying(true);
    const result = await list.refetch();
    setRetrying(false);
    // La tarjeta se va (llegó la lista, o la API niega): el foco, al título, si seguía ahí.
    const at = document.activeElement;
    const gone = result.isSuccess || result.error?.status === 403;
    if (gone && (at === document.body || at === retryButton.current)) heading.current?.focus();
  }

  async function loadMore() {
    setMore("busy");
    const result = await list.fetchNextPage();
    setMore((result.data?.pages.length ?? 0) > pages.length ? "arrived" : "idle");
  }

  let body;
  if (denied) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (list.data) {
    body =
      notes.length === 0 ? (
        <p className="text-muted">{t("empty")}</p>
      ) : (
        <ul aria-label={t("title")} className="flex flex-col gap-2">
          {notes.map((note) => (
            <li
              key={note.id}
              ref={note.id === firstOfLast ? fresh : undefined}
              tabIndex={note.id === firstOfLast ? -1 : undefined}
              className={note.is_pinned ? `${ROW} border-l-accent border-l-4` : ROW}
            >
              <ContactNote note={note} who={who} />
            </li>
          ))}
        </ul>
      );
  } else if ((list.isError && status !== 401) || retrying) {
    // Sin sesión (401), `Providers` ya lleva al login: queda el aviso de carga, sin error.
    body = (
      <div className="flex flex-col items-start gap-3">
        {list.isError ? (
          <p role="alert" className="text-danger">
            {errors(`api.${apiErrorKey(list.error)}`)}
          </p>
        ) : null}
        {/* Sigue montado mientras reintenta: otra pulsación no cancela la petición en vuelo. */}
        <Button
          ref={retryButton}
          variant="primary"
          size="lg"
          aria-disabled={retrying}
          onClick={() => retrying || void retry()}
        >
          {retrying ? t("loading") : errors("retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <>
      {canWrite && !denied ? (
        <section className="border-border bg-surface rounded-lg border p-4">
          <NoteForm
            key={round}
            focus={round > 0}
            write={create}
            send={(text, pinned) =>
              create.mutate({ orgSlug: slug, contactId, data: { body: text, is_pinned: pinned } })
            }
          />
        </section>
      ) : null}
      <section className="flex flex-col gap-3">
        <h2
          ref={heading}
          tabIndex={-1}
          className="text-lg font-semibold focus-visible:outline-none"
        >
          {t("title")}
        </h2>
        {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
        <p role="status" className={said ? "text-sm" : "sr-only"}>
          {said}
        </p>
        {body}
        {list.hasNextPage && !denied ? (
          <div className="flex items-start gap-3">
            <Button
              ref={moreButton}
              variant="accent"
              size="lg"
              aria-disabled={more === "busy"}
              onClick={() => more === "busy" || void loadMore()}
            >
              {more === "busy" ? t("loadingMore") : t("more")}
            </Button>
            {list.isFetchNextPageError && status !== 401 && more !== "busy" ? (
              <p role="alert" className="text-danger min-w-0 self-center text-sm">
                {errors(`api.${apiErrorKey(list.error)}`)}
              </p>
            ) : null}
          </div>
        ) : null}
      </section>
    </>
  );
}
