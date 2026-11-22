"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Bot, BotOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { isStepUpCancelled, STEP_UP_REQUIRED, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import {
  conversationAiExclusionSet,
  getConversationsListQueryKey,
  getConversationsRetrieveQueryKey,
} from "@/lib/api/client";
import type { Conversation, ConversationDetail } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

export const AI_MANAGE = "ai.manage";
const OUTLINE = "border-border min-h-11 border sm:min-h-9";

// Excluir la conversación de la IA (F8-13b): `PUT …/ai-exclusion/` con la versión leída. Excluirla
// lo puede quien ve la conversación (`conversations.view`, como la ruta); volver a incluirla solo se
// ofrece con `ai.manage` y pide el segundo factor reciente (`useStepUp`). Las dos, tras una
// confirmación que dice qué pasa, con el foco primero en «Cancelar». Da `button` (en la fila del
// encabezado; solo el icono por debajo de 1024 px), `state` (la marca, la pregunta y lo que pasó) y
// `dialog` (el del código). Ocultar el botón no autoriza nada: el servidor comprueba permiso,
// alcance, versión y segundo factor, y con la marca el copiloto responde 409 `AI_EXCLUDED`.
export function useAiExclusion(conversation: Conversation) {
  const t = useTranslations("inbox.aiExclusion");
  const errors = useTranslations("errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const { withStepUp, dialog } = useStepUp();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ error: boolean; text: string } | null>(null);
  const sending = useRef(false); // un clic doble no son dos peticiones
  const cancel = useRef<HTMLButtonElement>(null);
  const result = useRef<HTMLParagraphElement>(null);
  const excluded = Boolean(conversation.ai_excluded_at);
  useEffect(() => {
    if (asking) cancel.current?.focus(); // lo seguro primero
  }, [asking]);

  const offered = !excluded || permissions.some(({ code }) => code === AI_MANAGE);

  // Lo que se dice de un fallo, por `code` y nunca por el texto del servidor.
  const problem = (error: unknown) => {
    const failure = error instanceof ApiError ? error : null;
    if (failure?.code === "CONVERSATION_VERSION_CONFLICT") return t("stale");
    if (failure?.code === STEP_UP_REQUIRED) return t("stepUp"); // la repetición también lo pidió
    if (failure?.status === 403 || failure?.status === 404) return t("denied");
    return errors(`api.${apiErrorKey(error)}`);
  };

  async function confirm() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setSaid(null);
    const body = { excluded: !excluded, version: conversation.version };
    const detailKey = getConversationsRetrieveQueryKey(slug, conversation.id);
    try {
      const run = () => conversationAiExclusionSet(slug, conversation.id, body);
      const done = await (excluded ? withStepUp(run) : run());
      // El estado nuevo se ve al instante; la relectura lo confirma.
      client.setQueryData<ConversationDetail>(
        detailKey,
        (old) => old && { ...old, ai_excluded_at: done.ai_excluded_at, version: done.version },
      );
      setAsking(false);
      setSaid({ error: false, text: t(excluded ? "included" : "excluded") });
      setTimeout(() => result.current?.focus()); // el botón pulsado ya no está
    } catch (error) {
      if (!isStepUpCancelled(error)) setSaid({ error: true, text: problem(error) });
    } finally {
      sending.current = false;
      setBusy(false);
      void client.invalidateQueries({ queryKey: detailKey });
      void client.invalidateQueries({ queryKey: getConversationsListQueryKey(slug) });
    }
  }

  const label = t(excluded ? "include" : "exclude");
  const button = offered ? (
    <Button
      variant="ghost"
      className="border-border order-3 border max-lg:size-10 max-lg:px-0 lg:order-2"
      title={label}
      aria-expanded={asking}
      onClick={() => {
        setSaid(null);
        setAsking(true);
      }}
    >
      {excluded ? <Bot aria-hidden /> : <BotOff aria-hidden />}
      <span className="max-lg:sr-only">{label}</span>
    </Button>
  ) : null;
  const state = (
    <div className="order-5 flex basis-full flex-col gap-1 empty:hidden">
      {excluded ? (
        <p className="bg-accent/40 border-foreground/30 flex items-center gap-1.5 self-start rounded-[10px] border px-2 py-1 text-xs font-semibold lg:text-sm">
          <BotOff aria-hidden className="size-3.5 shrink-0" />
          {t("badge")}
        </p>
      ) : null}
      {asking && offered ? (
        <div
          role="group"
          aria-label={label}
          className="border-border flex flex-col gap-2 rounded-[10px] border p-2 text-sm"
        >
          <p>{t(excluded ? "includeQuestion" : "excludeQuestion")}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className={OUTLINE}
              aria-disabled={busy}
              onClick={() => !busy && setAsking(false)}
            >
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={() => void confirm()}
            >
              {busy ? t("saving") : t("confirm")}
            </Button>
          </div>
        </div>
      ) : null}
      {said ? (
        <p
          ref={result}
          tabIndex={-1}
          role={said.error ? "alert" : "status"}
          className={cn("text-sm focus-visible:outline-none", said.error && "text-danger")}
        >
          {said.text}
        </p>
      ) : null}
    </div>
  );
  return { button, state, dialog };
}
