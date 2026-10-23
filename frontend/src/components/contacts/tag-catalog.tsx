"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useLayoutEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { SectionNav } from "@/components/catalog/catalog-nav";
import { CursorList, type CursorListHandle } from "@/components/lists/cursor-list";
import { Button } from "@/components/ui/button";
import { getTagsListQueryKey, tagsList, useTagsUpdate } from "@/lib/api/client";
import type { Tag } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";

import { TagChip } from "./contact-tags";
import { TagForm } from "./tag-form";

// Las páginas de «Clientes»: la lista y el catálogo de etiquetas (F4-12c). Las dos leen con
// `contacts.view`, el permiso de la entrada. La ficha no lleva estas pestañas: tiene su ruta.
const PAGES = [
  { key: "list", path: "/clientes" },
  { key: "tags", path: "/clientes/etiquetas" },
] as const;
export const ContactsNav = () => <SectionNav section="contacts" pages={PAGES} />;

type Said = { text: string; alert?: boolean } | null;

// El catálogo de etiquetas de contactos (F4-12c): `GET …/tags/` (F4-11), activas o no, por orden
// de creación, con `CursorList`. Lo ve quien ve contactos; crear, renombrar, cambiar el color y
// desactivar o reactivar se ofrecen con `settings.manage`. Las etiquetas no se borran: una
// desactivada sigue en los contactos que la llevan y no se pone en otros.
export function TagCatalog() {
  const t = useTranslations("contactTags");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  // Comodidad: las escrituras se ofrecen a quien la API dijo que tiene `settings.manage`.
  const canManage = permissions.some((grant) => grant.code === "settings.manage");
  const listKey = [...getTagsListQueryKey(slug), "pages"];
  const list = useRef<CursorListHandle>(null);
  const [said, setSaid] = useState<Said>(null);
  function stale(name: string, here: boolean) {
    setSaid({ text: t("stale", { name }), alert: true });
    list.current?.refetch();
    if (here) list.current?.focusHeading();
  }
  const actions = {
    ...{ slug, listKey, onStale: stale },
    onAsk: () => setSaid(null),
    onDone: (text: string) => setSaid({ text }),
  };

  return (
    <CursorList<Tag>
      ref={list}
      section="contactTags"
      organization={organization.name}
      listKey={listKey}
      fetchPage={(cursor, signal) => tagsList(slug, cursor ? { cursor } : undefined, { signal })}
      notice={
        <>
          {canManage ? (
            <TagForm {...actions} />
          ) : (
            <p className="text-muted text-sm">{t("readOnly")}</p>
          )}
          {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
          <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
            {said && !said.alert ? said.text : ""}
          </p>
          {said?.alert ? (
            <p role="alert" className="text-danger wrap-anywhere">
              {said.text}
            </p>
          ) : null}
        </>
      }
      empty={<p className="text-muted">{t("empty")}</p>}
      rowClassName="grid gap-x-4 gap-y-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start"
    >
      {(row) => (
        <>
          <p className="flex min-w-0 flex-col items-start gap-1">
            <TagChip tag={row} />
            <span className="text-muted font-mono text-[13px]">{row.color}</span>
          </p>
          {canManage ? (
            <div className="flex flex-wrap items-start gap-2 sm:col-span-2">
              <TagForm {...actions} tag={row} />
              <TagStatusAction {...actions} tag={row} />
            </div>
          ) : null}
        </>
      )}
    </CursorList>
  );
}

// Desactivar o reactivar una etiqueta: un `PATCH` con `is_active`, un envío por pulsación. Sigue
// «Guardando…» hasta que vuelve la lista; un 404 lo explica la lista, y otro error, la fila.
function TagStatusAction({
  slug,
  listKey,
  tag,
  onAsk,
  onDone,
  onStale,
}: {
  slug: string;
  listKey: QueryKey;
  tag: Tag;
  onAsk: () => void;
  onDone: (text: string) => void;
  onStale: (name: string, here: boolean) => void;
}) {
  const t = useTranslations("contactTags.status");
  const errors = useTranslations("errors.api");
  const queryClient = useQueryClient();
  const button = useRef<HTMLButtonElement>(null);
  const sending = useRef(false);
  const update = useTagsUpdate({
    mutation: {
      networkMode: "always",
      onSuccess: async (row) => {
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        const key = row.is_active ? "activated" : "deactivated";
        onDone(t(stale ? "doneStale" : key, { name: row.name }));
      },
      onError: (error) => {
        if (error.status !== 404) return;
        const active = document.activeElement;
        onStale(tag.name, active === document.body || active === button.current);
      },
    },
  });
  // Sin sesión (401) sigue ocupado: `Providers` lleva al login.
  const busy = update.isPending || update.error?.status === 401;
  useLayoutEffect(() => {
    if (!busy) sending.current = false;
  });
  const error = update.error;
  const failed =
    error && ![401, 404].includes(error.status)
      ? error.code === "PERMISSION_DENIED"
        ? t("denied")
        : errors(apiErrorKey(error))
      : null;

  return (
    <div
      className="flex flex-col items-start gap-1"
      // Enter mantenido repite la pulsación: desactivaría y reactivaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      <Button
        ref={button}
        variant="ghost"
        className="border-border min-h-11 border sm:min-h-9"
        aria-label={t(tag.is_active ? "deactivateLabel" : "activateLabel", { name: tag.name })}
        aria-disabled={busy}
        onClick={() => {
          if (sending.current || busy) return;
          sending.current = true;
          onAsk();
          const data = { is_active: !tag.is_active };
          update.mutate({ orgSlug: slug, tagId: tag.id, data });
        }}
      >
        {busy ? t("busy") : t(tag.is_active ? "deactivate" : "activate")}
      </Button>
      {failed ? (
        <p role="alert" className="text-danger text-sm">
          {failed}
        </p>
      ) : null}
    </div>
  );
}
