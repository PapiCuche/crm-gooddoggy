"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { allPages } from "@/lib/all-pages";
import {
  quickRepliesDelete,
  quickRepliesList,
  quickRepliesUpdate,
  quickReplyCategoriesList,
} from "@/lib/api/client";
import type { ManagedQuickReplyCategory, QuickReply } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { type Ctx, Editor, ReplyForm } from "./quick-reply-form";

type Data = [ManagedQuickReplyCategory[], QuickReply[]];
type Said = { text: string; alert?: boolean } | null;
const BOX = "border-border bg-surface rounded-lg border";
const OFF = "border-border text-muted rounded-full border px-2 py-0.5 text-xs font-normal";
const page = (cursor?: string) => ({ limit: 200, ...(cursor ? { cursor } : {}) });

// Las respuestas rápidas (F6-08c): `…/quick-replies/` y `…/quick-reply-categories/` (F6-08a3/a4)
// enteras, activas o no, agrupadas por categoría en su orden; todo pide `quick_replies.manage`.
// Cada cambio vuelve a pedir la lista; un 409 `STALE_VERSION` o un 404 lo dice y recarga.
export function QuickReplyCatalog() {
  const t = useTranslations("quickReplies");
  const errors = useTranslations("errors");
  const { organization } = useTenant();
  const slug = organization.slug;
  const queryClient = useQueryClient();
  const key = ["quickRepliesScreen", slug];
  const heading = useRef<HTMLHeadingElement>(null);
  const [said, setSaid] = useState<Said>(null);
  const list = useQuery<Data, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) =>
      Promise.all([
        allPages((c) => quickReplyCategoriesList(slug, page(c), { signal })),
        allPages((c) => quickRepliesList(slug, page(c), { signal })),
      ]),
    gcTime: 0,
  });
  const off = (row: { is_active: boolean }) =>
    row.is_active ? null : <span className={OFF}>{t("inactive")}</span>;
  const reload = () => queryClient.invalidateQueries({ queryKey: key });
  // La fila que se va (borrada o movida) se lleva el foco: al título, tras pintar la lista nueva.
  const refocus = useRef(false);
  useEffect(() => {
    if (refocus.current && document.activeElement === document.body) heading.current?.focus();
    refocus.current = false;
  }, [list.data]);
  const ctx: Ctx = {
    slug,
    clear: () => setSaid(null),
    say: (text) => setSaid({ text }),
    after: async (text) => {
      refocus.current = true;
      await reload();
      const failed = queryClient.getQueryState(key)?.status === "error";
      setSaid({ text: failed ? t("reloadFailed") : text });
    },
    fail: (error, name) => {
      if (error.status === 401) return; // `Providers` lleva al login
      const reason =
        error.code === "PERMISSION_DENIED" ? t("form.denied") : errors(`api.${apiErrorKey(error)}`);
      const stale = error.status === 404 || error.code === "STALE_VERSION";
      const text = stale ? t("stale", { name }) : t("failed", { name, reason });
      setSaid({ text, alert: true });
      if (stale) void reload();
    },
  };

  let body: ReactNode;
  if (list.error?.status === 403) {
    body = <p role="alert">{t("denied")}</p>;
  } else if (list.data) {
    const [categories, replies] = list.data;
    const ordered = categories.toSorted(
      (a, b) => a.sort_order - b.sort_order || a.name.localeCompare(b.name, "es"),
    );
    body = (
      <div className="flex max-w-4xl min-w-0 flex-col gap-6">
        {ordered.some((c) => c.is_active) ? (
          <Editor label={t("form.open")} accent onOpen={ctx.clear}>
            {(close) => <ReplyForm ctx={ctx} categories={ordered} close={close} />}
          </Editor>
        ) : (
          <p className="text-muted">{t("noCategories")}</p>
        )}
        {ordered.map((category) => {
          const rows = replies.filter((r) => r.category_id === category.id);
          return (
            <section key={category.id} className="flex flex-col gap-2">
              <h2 className="flex flex-wrap items-center gap-2 font-semibold">
                <bdi>{category.name}</bdi>
                {off(category)}
              </h2>
              {rows.length ? (
                <ul aria-label={category.name} className="flex flex-col gap-2">
                  {rows.map((reply) => {
                    const name = `/${reply.shortcut}`;
                    const flip = { is_active: !reply.is_active, version: reply.version };
                    return (
                      <li key={reply.id} className={cn(BOX, "flex flex-col gap-1 p-4")}>
                        <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                          <span className="font-mono font-medium wrap-anywhere">{name}</span>
                          <bdi className="min-w-0 wrap-anywhere">{reply.title}</bdi>
                          {off(reply)}
                        </p>
                        <p className="text-muted line-clamp-3 text-sm wrap-anywhere whitespace-pre-wrap">
                          <bdi>{reply.body}</bdi>
                        </p>
                        <p className="text-muted text-xs">
                          {t("uses", { count: reply.usage_count })}
                        </p>
                        <div className="mt-2 flex flex-wrap items-start gap-2">
                          <Editor
                            label={t("form.edit")}
                            aria={t("form.editLabel", { name })}
                            onOpen={ctx.clear}
                          >
                            {(close) => (
                              <ReplyForm
                                ctx={ctx}
                                reply={reply}
                                categories={ordered}
                                close={close}
                              />
                            )}
                          </Editor>
                          <Act
                            ctx={ctx}
                            name={name}
                            active={reply.is_active}
                            run={() => quickRepliesUpdate(slug, reply.id, flip)}
                          />
                          <Act
                            ctx={ctx}
                            name={name}
                            run={() => quickRepliesDelete(slug, reply.id)}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="text-muted text-sm">{t("emptyCategory")}</p>
              )}
            </section>
          );
        })}
      </div>
    );
  } else if (list.isError && list.error.status !== 401) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {errors(`api.${apiErrorKey(list.error)}`)}
        </p>
        <Button variant="primary" size="lg" onClick={() => void list.refetch()}>
          {errors("retry")}
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
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] focus-visible:outline-none"
        >
          {t("title")}
        </h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {/* Montado desde el principio: un lector de pantalla lo anuncia cuando cambia. */}
      <p role="status" className={said && !said.alert ? "text-sm wrap-anywhere" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger wrap-anywhere">
          {said.text}
        </p>
      ) : null}
      {body}
    </section>
  );
}

// Desactivar o reactivar (con `active`), o borrar tras confirmar (sin él): una escritura por
// pulsación, «Guardando…» hasta que vuelve la lista.
function Act({
  ctx,
  name,
  active,
  run,
}: {
  ctx: Ctx;
  name: string;
  active?: boolean;
  run: () => Promise<unknown>;
}) {
  const t = useTranslations("quickReplies.act");
  const what = active === undefined ? "delete" : active ? "deactivate" : "activate";
  const [asking, setAsking] = useState(false);
  const cancel = useRef<HTMLButtonElement>(null);
  const write = useMutation<unknown, ApiError>({
    mutationFn: run,
    networkMode: "always",
    onSuccess: () => ctx.after(t(`${what}d` as "deleted", { name })),
    onError: (error) => ctx.fail(error, name),
  });
  const busy = write.isPending || write.error?.status === 401;
  useEffect(() => {
    if (asking) cancel.current?.focus(); // lo seguro
  }, [asking]);
  const go = () => {
    if (busy) return;
    ctx.clear();
    if (what === "delete" && !asking) return setAsking(true);
    write.mutate();
  };

  return (
    <div
      className={cn("flex flex-col items-start gap-2", asking && "w-full")}
      // Enter mantenido repite la pulsación: desactivaría y reactivaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {asking ? <p className="text-sm wrap-anywhere">{t("ask", { name })}</p> : null}
      <div className="flex flex-wrap gap-2">
        {asking ? (
          <Button
            ref={cancel}
            className="border-border min-h-11 border sm:min-h-9"
            aria-disabled={busy}
            onClick={() => busy || setAsking(false)}
          >
            {t("cancel")}
          </Button>
        ) : null}
        <Button
          variant="ghost"
          className={cn("border-border min-h-11 border sm:min-h-9", asking && "text-danger")}
          aria-label={asking ? undefined : t(`${what}Label`, { name })}
          aria-disabled={busy}
          onClick={go}
        >
          {busy ? t("busy") : asking ? t("confirm") : t(what)}
        </Button>
      </div>
    </div>
  );
}
