"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { channelsWhatsappDisconnect } from "@/lib/api/client";
import { Plain } from "@/components/inbox/plain";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

// Desconectar una cuenta (F7-07b): `DELETE …/channels/whatsapp/{id}/` tras una confirmación que
// dice lo que pasa: deja de recibir y enviar, y reconectar es crear una cuenta nueva. El foco
// va a «Cancelar» (lo seguro) al abrir y la lista se vuelve a pedir al terminar.
export function DisconnectAccount({
  slug,
  accountId,
  name,
  onDisconnected,
}: {
  slug: string;
  accountId: string;
  name: string;
  onDisconnected: () => void;
}) {
  const t = useTranslations("channels.disconnect");
  const errors = useTranslations("channels.errors");
  const api = useTranslations("errors.api");
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (asking) cancel.current?.focus(); // lo seguro primero
  }, [asking]);

  function confirm() {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    channelsWhatsappDisconnect(slug, accountId).then(
      () => {
        setBusy(false);
        onDisconnected(); // la cuenta ya no está: su tarjeta se va con la lista nueva
      },
      (error: ApiError) => {
        setBusy(false);
        if (error.status === 401) return; // `Providers` lleva al login
        // La cuenta ya no existe (404): se vuelve a leer la lista y se dice.
        if (error.status === 404) onDisconnected();
        setProblem(
          error.status === 403
            ? errors("denied")
            : error.status === 404
              ? t("gone")
              : api(apiErrorKey(error)),
        );
      },
    );
  }

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {asking ? (
        <div
          role="group"
          aria-label={t("title", { name })}
          className="border-danger flex w-full max-w-xl flex-col gap-3 rounded-lg border p-3"
        >
          {/* El nombre lo escribió otra persona: aislado (`Plain`) para que un carácter de dirección no
              reordene lo que lo rodea en la pregunta de una acción que no se deshace. */}
          <p className="wrap-anywhere">
            {t.rich("question", { name, n: (chunks) => <Plain>{chunks}</Plain> })}
          </p>
          <p className="text-muted text-sm">{t("effect")}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className="border-border min-h-11 border sm:min-h-9"
              aria-disabled={busy}
              onClick={() => {
                if (busy) return;
                setAsking(false);
                setTimeout(() => trigger.current?.focus());
              }}
            >
              {t("cancel")}
            </Button>
            <Button
              variant="primary"
              className="min-h-11 sm:min-h-9"
              aria-disabled={busy}
              onClick={confirm}
            >
              {busy ? t("busy") : t("confirm")}
            </Button>
          </div>
        </div>
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={() => {
            setProblem(null);
            setAsking(true);
          }}
        >
          {t("open")}
        </Button>
      )}
      {problem ? (
        <p role="alert" className="text-danger text-sm">
          {problem}
        </p>
      ) : null}
    </div>
  );
}
