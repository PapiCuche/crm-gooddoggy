"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Bot, BotOff } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

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
const OUTLINE = "border-border min-h-11 border lg:min-h-9";
// Lo que se leyó al abrir la confirmación: la pregunta y la petición salen de aquí, nunca del
// estado vivo (una relectura por tiempo real no puede cambiar lo que se confirma).
type Asked = { excluded: boolean; version: number };
// `at`: la marca a la que se refiere un acierto; si cambia por otra vía, ya no se muestra.
type Said = { error: boolean; text: string; at?: string | null };

// Excluir la conversación de la IA (F8-13b): `PUT …/ai-exclusion/` con la versión leída. Excluirla
// lo puede quien ve la conversación (`conversations.view`, como la ruta); volver a incluirla solo se
// ofrece con `ai.manage` y pide el segundo factor reciente (`useStepUp`). Las dos, tras una
// confirmación que dice qué pasa, con el foco primero en «Cancelar». Si la marca cambia con la
// confirmación abierta, o la API responde que la versión es vieja, se cierra sin enviar nada más:
// para decidir de nuevo hay que volver a abrirla. Da `button` (en la fila del encabezado; solo el
// icono por debajo de 1024 px), `state` (la marca, la pregunta y lo que pasó) y `dialog` (el del
// código). Ocultar el botón no autoriza nada: el servidor comprueba permiso, alcance, versión y
// segundo factor, y con la marca el copiloto responde 409 `AI_EXCLUDED`.
export function useAiExclusion(conversation: Conversation) {
  const t = useTranslations("inbox.aiExclusion");
  const errors = useTranslations("errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const client = useQueryClient();
  const { withStepUp, dialog } = useStepUp();
  const question = useId();
  const [asking, setAsking] = useState<Asked | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);
  const sending = useRef(false); // un clic doble no son dos peticiones
  const opener = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const result = useRef<HTMLParagraphElement>(null);
  const at = conversation.ai_excluded_at ?? null;
  const excluded = at !== null;
  const open = asking !== null;
  const opened = useRef(false);
  useEffect(() => {
    // Al abrir, el foco va a «Cancelar» (lo seguro); al cerrar, vuelve a quien la abrió o, si ya
    // no está, al resultado.
    if (open) cancel.current?.focus();
    else if (opened.current && document.activeElement === document.body) {
      (opener.current ?? result.current)?.focus();
    }
    opened.current = open;
  }, [open]);
  // Otra persona cambió la marca con la confirmación abierta (una relectura por tiempo real o por
  // «Actualizar»): lo leído ya no vale y se cierra en este mismo render, antes de poder confirmar.
  if (asking && asking.excluded !== excluded && !busy) {
    setAsking(null);
    setSaid({ error: true, text: t("stale") });
  }

  const offered = !excluded || permissions.some(({ code }) => code === AI_MANAGE);

  // Lo que se dice de un fallo, por `code` y nunca por el texto del servidor.
  const problem = (error: unknown) => {
    const failure = error instanceof ApiError ? error : null;
    if (failure?.code === "CONVERSATION_VERSION_CONFLICT") return t("stale");
    if (failure?.code === STEP_UP_REQUIRED) return t("stepUp"); // la repetición también lo pidió
    if (failure?.code === "AI_CALL_IN_PROGRESS") return t("busy"); // F8-13c: nada cambió
    if (failure?.code === "CONVERSATION_BUSY") return t("rowBusy"); // F8-13d: nada cambió
    if (failure?.status === 403 || failure?.status === 404) return t("denied");
    return errors(`api.${apiErrorKey(error)}`);
  };

  async function confirm(asked: Asked) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setSaid(null);
    const body = { excluded: !asked.excluded, version: asked.version };
    const detailKey = getConversationsRetrieveQueryKey(slug, conversation.id);
    try {
      const run = () => conversationAiExclusionSet(slug, conversation.id, body);
      const done = await (asked.excluded ? withStepUp(run) : run());
      // El estado nuevo se ve al instante; la relectura lo confirma.
      client.setQueryData<ConversationDetail>(
        detailKey,
        (old) => old && { ...old, ai_excluded_at: done.ai_excluded_at, version: done.version },
      );
      setAsking(null);
      const text = t(asked.excluded ? "included" : "excluded");
      setSaid({ error: false, text, at: done.ai_excluded_at });
      setTimeout(() => result.current?.focus()); // el botón pulsado ya no está
    } catch (error) {
      if (!isStepUpCancelled(error)) {
        setSaid({ error: true, text: problem(error) });
        // Con la versión vieja no se deja lista otra petición: se vuelve a abrir con lo releído.
        if (error instanceof ApiError && error.code === "CONVERSATION_VERSION_CONFLICT") {
          setAsking(null);
        }
      }
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
      ref={opener}
      variant="ghost"
      className="border-border order-3 border max-lg:size-11 max-lg:px-0 max-sm:group-data-composing:hidden lg:order-2"
      title={label}
      aria-expanded={open}
      onClick={() => {
        if (sending.current) return;
        setSaid(null);
        setAsking({ excluded, version: conversation.version });
      }}
    >
      {excluded ? <Bot aria-hidden /> : <BotOff aria-hidden />}
      <span className="max-lg:sr-only">{label}</span>
    </Button>
  ) : null;
  const shown = said && (said.error || said.at === at) ? said : null;
  const state = (
    <div className="order-5 flex basis-full flex-col gap-1 empty:hidden">
      {excluded ? (
        <p className="bg-accent/40 border-foreground/30 flex items-center gap-1.5 self-start rounded-[10px] border px-2 py-1 text-xs font-semibold max-sm:group-data-composing:hidden lg:text-sm">
          <BotOff aria-hidden className="size-3.5 shrink-0" />
          {t("badge")}
        </p>
      ) : null}
      {asking ? (
        <div
          role="group"
          aria-label={t(asking.excluded ? "include" : "exclude")}
          aria-describedby={question}
          className="border-border flex flex-col gap-2 rounded-[10px] border p-2 text-sm"
        >
          <p id={question}>{t(asking.excluded ? "includeQuestion" : "excludeQuestion")}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className={OUTLINE}
              aria-disabled={busy}
              onClick={() => sending.current || setAsking(null)}
            >
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              className="min-h-11 lg:min-h-9"
              aria-disabled={busy}
              onClick={() => void confirm(asking)}
            >
              {busy ? t("saving") : t("confirm")}
            </Button>
          </div>
        </div>
      ) : null}
      {shown ? (
        <p
          ref={result}
          tabIndex={-1}
          role={shown.error ? "alert" : "status"}
          className={cn("text-sm focus-visible:outline-none", shown.error && "text-danger")}
        >
          {shown.text}
        </p>
      ) : null}
    </div>
  );
  return { button, state, dialog };
}
