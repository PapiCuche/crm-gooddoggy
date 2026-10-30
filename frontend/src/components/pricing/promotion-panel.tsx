"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTimeZone, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  getPromotionsListQueryKey,
  promotionsCancel,
  promotionsPublish,
  promotionsRetrieve,
} from "@/lib/api/client";
import type { Promotion } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { KINDS, useTargetNames } from "./promotion-targets";
import { stamp, ZONE } from "./zoned";

type Action = "publish" | "cancel";
type Props = {
  slug: string;
  promotion: Promotion;
  canManage: boolean;
  listNames: ReadonlyMap<string, string>;
  discount: (type: Promotion["discount_type"], value: string, currency: string) => string;
  onChanged: (promotion: Promotion) => void;
  onClose: () => void;
};

// El detalle de una promoción (F3-48f) en un `<dialog>` modal, como el historial de precios. Con
// `prices.manage`, «Publicar» y «Cancelar» piden confirmación; un 409 se explica y se relee.
export function PromotionPanel({
  slug,
  promotion,
  canManage,
  listNames,
  discount,
  onChanged,
  onClose,
}: Props) {
  const t = useTranslations("promotions");
  const errors = useTranslations("errors");
  const zone = useTimeZone() ?? ZONE;
  const queryClient = useQueryClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const confirmHeading = useRef<HTMLParagraphElement>(null);
  const id = useId();
  const [ask, setAsk] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  const targets = useTargetNames(slug, promotion.items);
  const p = promotion;

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    heading.current?.focus(); // el título primero: el lector de pantalla dice qué se abrió
  }, []);
  useEffect(() => {
    if (ask) confirmHeading.current?.focus();
  }, [ask]);

  async function run(action: Action) {
    setBusy(true);
    setSaid(null);
    const send = action === "publish" ? promotionsPublish : promotionsCancel;
    try {
      onChanged(await send(slug, p.id));
      setSaid({ text: t(action === "publish" ? "panel.published" : "panel.cancelled") });
    } catch (error) {
      const failure = error as ApiError;
      setSaid({ text: errors(`api.${apiErrorKey(failure)}`), alert: true });
      if (failure.status === 409 || failure.status === 404) {
        // Otro la cambió antes: se muestra su estado de ahora.
        try {
          onChanged(await promotionsRetrieve(slug, p.id));
        } catch {}
      }
    } finally {
      setBusy(false);
      setAsk(null);
      void queryClient.invalidateQueries({ queryKey: getPromotionsListQueryKey(slug) });
      heading.current?.focus();
    }
  }

  const label = (item: Promotion["items"][number]) => {
    const kind = KINDS.find((key) => item[key])!;
    const key = item[kind]!;
    const name = targets.names?.get(key) ?? `#${key.slice(0, 8)}`;
    const own = item.override_value
      ? ` · ${t("panel.override", { value: discount(p.discount_type, item.override_value, p.currency) })}`
      : "";
    return `${t(`panel.${kind.replace("_id", "") as "variant"}`)}: ${name}${own}`;
  };
  const from = stamp(p.starts_at, zone);
  const to = stamp(p.ends_at, zone);
  const field = (title: string, body: string) => (
    <div className="flex flex-col gap-1">
      <h3 className="text-muted text-[13px] tracking-wide uppercase">{title}</h3>
      <p className="text-sm wrap-anywhere whitespace-pre-line">{body}</p>
    </div>
  );
  const button = "min-h-11 sm:min-h-9";

  return (
    <dialog
      ref={dialog}
      aria-labelledby={id}
      className="bg-surface text-foreground border-border fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-none w-full max-w-none overflow-y-auto border-l p-0 backdrop:bg-black/40 sm:max-w-md"
      onClose={onClose}
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
      onClick={(event) => {
        const box = dialog.current!.getBoundingClientRect();
        const { clientX: x, clientY: y } = event;
        const inside = x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
        if (event.target === dialog.current && !inside) dialog.current.close();
      }}
    >
      <div className="flex min-h-full flex-col gap-4 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <h2
              id={id}
              ref={heading}
              tabIndex={-1}
              className="text-lg font-semibold wrap-anywhere focus-visible:outline-none"
            >
              {p.name}
            </h2>
            <p className="text-sm font-medium">
              {t(`status.${p.status}`)} ·{" "}
              <span className="tabular-nums">{discount(p.discount_type, p.value, p.currency)}</span>
            </p>
            <p className="text-muted text-sm tabular-nums">{t("window", { from, to })}</p>
          </div>
          <Button
            variant="ghost"
            className="size-11 shrink-0 text-xl"
            aria-label={t("panel.close")}
            onClick={() => dialog.current?.close()}
          >
            <span aria-hidden>×</span>
          </Button>
        </div>
        {/* Montado desde el principio: un lector de pantalla anuncia lo que cambia. */}
        <p
          role={said?.alert ? "alert" : "status"}
          className={said ? (said.alert ? "text-danger text-sm" : "text-sm") : "sr-only"}
        >
          {said?.text ?? ""}
        </p>
        {canManage && ask ? (
          <div className="border-border flex flex-col gap-3 rounded-lg border p-3">
            <p ref={confirmHeading} tabIndex={-1} className="text-sm focus-visible:outline-none">
              <span className="font-medium">
                {t(ask === "publish" ? "panel.publish" : "panel.cancel")}:{" "}
              </span>
              {ask === "publish" ? t("panel.publishAsk", { from, to }) : t("panel.cancelAsk")}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                variant={ask === "publish" ? "primary" : "accent"}
                className={button}
                aria-disabled={busy}
                onClick={() => busy || void run(ask)}
              >
                {busy
                  ? t("panel.busy")
                  : t(ask === "publish" ? "panel.publishConfirm" : "panel.cancelConfirm")}
              </Button>
              <Button
                variant="ghost"
                className={`${button} border-border border`}
                aria-disabled={busy}
                onClick={() => busy || setAsk(null)}
              >
                {t("panel.back")}
              </Button>
            </div>
          </div>
        ) : canManage && p.status !== "CANCELLED" ? (
          <div className="flex flex-wrap gap-2">
            {p.status === "DRAFT" ? (
              <Button variant="primary" className={button} onClick={() => setAsk("publish")}>
                {t("panel.publish")}
              </Button>
            ) : null}
            <Button
              variant="ghost"
              className={`${button} border-border border`}
              onClick={() => setAsk("cancel")}
            >
              {t("panel.cancel")}
            </Button>
          </div>
        ) : null}
        {field(t("panel.terms"), p.terms || t("panel.none"))}
        {field(t("panel.description"), p.description || t("panel.none"))}
        <div className="flex flex-col gap-1">
          <h3 className="text-muted text-[13px] tracking-wide uppercase">{t("panel.targets")}</h3>
          <ul className="flex flex-col gap-1 text-sm">
            {p.items.map((item, index) => (
              <li key={index} className="wrap-anywhere">
                {label(item)}
              </li>
            ))}
          </ul>
          {targets.failed ? <p className="text-muted text-sm">{t("panel.namesFailed")}</p> : null}
        </div>
        {field(
          t("panel.lists"),
          p.price_list_ids.map((one) => listNames.get(one) ?? `#${one.slice(0, 8)}`).join(", "),
        )}
        {field(t("panel.channels"), p.channels ? p.channels.join(", ") : t("panel.allChannels"))}
        {field(t("panel.ai"), t(p.ai_exposable ? "panel.yes" : "panel.no"))}
      </div>
    </dialog>
  );
}
