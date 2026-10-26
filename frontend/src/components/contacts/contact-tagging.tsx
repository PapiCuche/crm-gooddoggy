"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Choice } from "@/components/catalog/product-create";
import { useReads } from "@/components/inventory/cell-picker";
import { Button } from "@/components/ui/button";
import { getContactsRetrieveQueryKey, useContactsTag, useContactsUntag } from "@/lib/api/client";
import type { Contact } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { TagChip, TagList, useTagCatalog } from "./contact-tags";

type Said = { text: string; alert?: boolean } | null;

// El grupo «Etiquetas» del «Resumen» (F4-12). Con `contacts.update` (cualquier alcance) se ponen
// y se quitan (`PUT` y `DELETE …/contacts/{id}/tags/{tag_id}/`, F4-11b): la API responde 204
// sin cuerpo, así que la ficha se vuelve a pedir y lo dice cuando llega. Un envío por
// pulsación. Un 404 (el contacto salió de su alcance de edición o se borró) relee la ficha, que
// lleva a «no encontrada» si ya no la ve; `TAG_INACTIVE` relee el catálogo. Quién puede lo decide
// la API.
export function ContactTags({ contact, canTag }: { contact: Contact; canTag: boolean }) {
  const t = useTranslations("contacts.tags");
  const errors = useTranslations("errors.api");
  const id = useId();
  const slug = useTenant().organization.slug;
  const client = useQueryClient();
  const cardKey = getContactsRetrieveQueryKey(slug, contact.id);
  // El catálogo solo hace falta para poner: sin `contacts.update` no se pide.
  const catalog = useTagCatalog(slug, canTag);
  const { failed, retry } = useReads();
  const [chosen, setChosen] = useState("");
  const [missing, setMissing] = useState(false);
  const [said, setSaid] = useState<Said>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const sending = useRef(false);
  // Quitada, su botón se va con la ficha nueva: el foco, al título del grupo si se quedó sin
  // sitio (no si el usuario ya está en otra parte).
  const refocus = useRef(false);
  useEffect(() => {
    if (refocus.current && document.activeElement === document.body) heading.current?.focus();
    refocus.current = false;
  }, [contact.tags]);
  const worn = new Set(contact.tags.map((tag) => tag.id));
  const offered = (catalog.data ?? []).filter((tag) => tag.is_active && !worn.has(tag.id));
  const picked = offered.find((tag) => tag.id === chosen);
  const nameOf = (tagId: string) =>
    [...contact.tags, ...(catalog.data ?? [])].find((tag) => tag.id === tagId)?.name ?? "";

  async function done(key: "added" | "removed", tagId: string) {
    const name = nameOf(tagId);
    if (key === "removed") refocus.current = true;
    // Sigue ocupado hasta que vuelve la ficha: la etiqueta aparece (o se va) con el aviso.
    await client.invalidateQueries({ queryKey: cardKey });
    const stale = client.getQueryState(cardKey)?.status === "error";
    if (key === "added") setChosen("");
    setSaid({ text: t(stale ? "doneStale" : key, { name }) });
  }
  async function failedWith(error: ApiError, tagId: string) {
    if (error.status === 401) return; // `Providers` lleva al login
    if (error.status === 404) {
      setSaid({ text: t("stale"), alert: true });
      await client.invalidateQueries({ queryKey: cardKey });
    } else if (error.code === "TAG_INACTIVE") {
      setSaid({ text: t("inactiveError", { name: nameOf(tagId) }), alert: true });
      setChosen("");
      await catalog.refetch();
    } else {
      const text =
        error.code === "PERMISSION_DENIED" ? t("writeDenied") : errors(apiErrorKey(error));
      setSaid({ text, alert: true });
    }
  }
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const tag = useContactsTag({
    mutation: {
      networkMode: "always",
      onSuccess: (_, { tagId }) => done("added", tagId),
      onError: (error, { tagId }) => failedWith(error, tagId),
    },
  });
  const untag = useContactsUntag({
    mutation: {
      networkMode: "always",
      onSuccess: (_, { tagId }) => done("removed", tagId),
      onError: (error, { tagId }) => failedWith(error, tagId),
    },
  });
  // Sin sesión (401) sigue ocupado: `Providers` lleva al login.
  const busy =
    tag.isPending || untag.isPending || [tag, untag].some((one) => one.error?.status === 401);
  // La marca se suelta cuando la pantalla ya enseña el resultado, no al llegar la respuesta.
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });

  function start(write: typeof tag | typeof untag, tagId: string) {
    if (sending.current || busy) return;
    sending.current = true;
    setSaid(null);
    write.mutate({ orgSlug: slug, contactId: contact.id, tagId });
  }
  function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending.current || busy) return;
    if (!picked) return setMissing(true);
    start(tag, picked.id);
  }
  const removing = untag.isPending ? untag.variables?.tagId : undefined;

  return (
    <section
      aria-labelledby={`${id}-tags`}
      className="border-border bg-surface flex flex-col gap-4 rounded-lg border p-4"
      // Enter mantenido repite la pulsación: pondría o quitaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <h2
        ref={heading}
        id={`${id}-tags`}
        tabIndex={-1}
        className="text-lg font-semibold focus-visible:outline-none"
      >
        {t("label")}
      </h2>
      {contact.tags.length === 0 ? <p className="text-muted">{t("none")}</p> : null}
      {contact.tags.length > 0 && !canTag ? <TagList tags={contact.tags} /> : null}
      {contact.tags.length > 0 && canTag ? (
        <ul aria-label={t("label")} className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
          {contact.tags.map((one) => (
            <li key={one.id} className="flex max-w-full items-center gap-1">
              <TagChip tag={one} />
              <Button
                variant="ghost"
                className="min-h-11 shrink-0 px-3 sm:min-h-9"
                aria-label={t("removeLabel", { name: one.name })}
                aria-disabled={busy}
                onClick={() => start(untag, one.id)}
              >
                {removing === one.id ? t("removing") : t("remove")}
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
      {canTag ? (
        <form
          noValidate
          onSubmit={add}
          aria-label={t("add")}
          aria-busy={busy}
          className="flex flex-col gap-2 sm:flex-row sm:items-end"
        >
          <div className="flex min-w-0 flex-col gap-2 sm:w-72">
            <Choice
              label={t("add")}
              value={picked?.id ?? ""}
              busy={busy}
              error={missing && !picked ? t("required") : undefined}
              hint={
                failed(catalog, t("denied")) ??
                (catalog.isPending
                  ? t("loading")
                  : catalog.isSuccess && offered.length === 0
                    ? t("noneLeft")
                    : undefined)
              }
              onChange={(next) => {
                setChosen(next);
                setMissing(false);
              }}
              options={[
                ["", t("choose")],
                ...offered.map(({ id: key, name }): [string, string] => [key, name]),
              ]}
            />
            {retry(catalog)}
          </div>
          <Button
            type="submit"
            variant="accent"
            className="min-h-11 self-start sm:min-h-9 sm:self-auto"
            aria-disabled={busy}
          >
            {tag.isPending ? t("adding") : t("addButton")}
          </Button>
        </form>
      ) : null}
      {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
      <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm wrap-anywhere">
          {said.text}
        </p>
      ) : null}
    </section>
  );
}
