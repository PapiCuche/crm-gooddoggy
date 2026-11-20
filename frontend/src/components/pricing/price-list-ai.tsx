"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId, useRef, useState } from "react";

import { aiError, toLogin } from "@/components/ai/ai-key-actions";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { priceListsAiExposable } from "@/lib/api/client";
import type { PriceList } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

export const AI_MANAGE = "ai.manage";
// La interna nunca la cita la IA (04 §K.2): la API la rechaza por su tipo o por quién la ve.
export const isInternal = (row: PriceList) =>
  row.kind === "INTERNAL" || row.required_permission === "prices.view_internal";

// Si la IA puede citar los precios de la lista (F8-07b, D-F8-06): lo decide quien tiene
// `ai.manage`, con `PUT …/ai-exposable/`, el segundo factor si la API lo pide y el tope por
// persona de las escrituras de IA. Lo que pasó lo dice la vista (`onDone`, `onAlert`, `onStale`).
export function PriceListAi(props: {
  slug: string;
  row: PriceList;
  listKey: QueryKey;
  onDone: (text: string) => void;
  onAlert: (text: string) => void;
  onStale: (name: string, here: boolean) => void;
}) {
  const { slug, row } = props;
  const t = useTranslations("priceLists.ai");
  const own = useTranslations("ai.errors");
  const queryClient = useQueryClient();
  const { withStepUp, dialog } = useStepUp();
  const [busy, setBusy] = useState(false);
  const sending = useRef(false); // dos pulsaciones seguidas no son dos peticiones
  const hint = useId();

  async function change() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    const wanted = !row.ai_exposable;
    try {
      await withStepUp(() => priceListsAiExposable(slug, row.id, { ai_exposable: wanted }));
      await queryClient.invalidateQueries({ queryKey: props.listKey });
      props.onDone(t(wanted ? "on" : "off", { name: row.name }));
    } catch (caught) {
      const error = caught as ApiError;
      const cancelled: boolean = isStepUpCancelled(error); // cancelar el código no es un fallo
      if (cancelled || toLogin(error)) return;
      if (error.code === "NOT_FOUND") return props.onStale(row.name, true);
      const known = error.code === "PERMISSION_DENIED" || error.code === "VALIDATION_ERROR";
      props.onAlert(
        known ? t(error.code) : (aiError(error, own)?.text ?? t("failed", { name: row.name })),
      );
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <label className="flex min-h-11 items-center gap-2 text-sm sm:min-h-9">
        <input
          type="checkbox"
          className="size-4 shrink-0"
          checked={row.ai_exposable}
          aria-disabled={busy}
          aria-label={t("labelFor", { name: row.name })}
          aria-describedby={hint}
          onChange={() => void change()}
        />
        {t("label")}
      </label>
      <p id={hint} className="text-muted text-sm">
        {t("hint")}
      </p>
      {dialog}
    </div>
  );
}
