"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import {
  ALERT,
  type Phase,
  refusedField,
  useRejection,
  useStall,
} from "@/components/account/mfa-enrollment";
import { LINK } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthMfaStepUp } from "@/lib/api/client";

const SECURITY = "/cuenta/seguridad";

type Props = { onVerified: () => void; onCancel: () => void };

// Pide el código del segundo factor (ADR-003 §4, ADR-023 §7) y llama a `POST /auth/mfa/step-up/`.
// Es un `<dialog>` modal nativo: atrapa el foco, Escape cancela y la página queda inerte. El
// código vive solo en el campo y en la petición: tras responder, `reset()` lo borra de la caché
// de mutaciones (`variables`), también con el diálogo abierto tras un fallo. Quien
// lo monta (`useStepUp`) decide qué pasa al verificar o cancelar; aquí no se reintenta nada.
export function StepUpDialog({ onVerified, onCancel }: Props) {
  const t = useTranslations("stepUp");
  const rejection = useRejection();
  const { mutate, reset } = useAuthMfaStepUp({ mutation: { networkMode: "always", gcTime: 0 } });
  const dialog = useRef<HTMLDialogElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [recovery, setRecovery] = useState(false);
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const [noFactor, setNoFactor] = useState(false);
  const title = useId();
  const sending = phase.name === "sending";
  useStall(sending, reset, setPhase);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    if (!dialog.current?.open) dialog.current?.showModal();
    return () => {
      if (before?.isConnected) before.focus();
    }; // cerrar sin `close()` no devuelve el foco
  }, []);
  useEffect(() => field.current?.focus(), [recovery, noFactor]);
  const refused = refusedField(phase, "code");
  useEffect(() => {
    if (refused) field.current?.select();
  }, [refused]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending) return;
    const code = String(new FormData(event.currentTarget).get("code") ?? "").replace(/[\s-]/g, "");
    if (!code) {
      setPhase({ name: "missing" });
      field.current?.focus();
      return;
    }
    setPhase({ name: "sending" });
    mutate(
      { data: { code } },
      {
        onSuccess: () => {
          reset(); // el código no queda en la caché de mutaciones
          onVerified();
        },
        onError: (error) => {
          reset(); // ni tras un fallo: el diálogo sigue abierto y `variables` lo guardaría
          if (error.code === "MFA_NOT_ENABLED") setNoFactor(true);
          else setPhase({ name: "failed", error });
        },
      },
    );
  }

  let codeError: string | undefined;
  if (phase.name === "missing") codeError = t("codeRequired");
  else if (refused) codeError = t(recovery ? "recoveryInvalid" : "codeInvalid");
  let formError: string | null = null;
  if (phase.name === "stalled") formError = t("stalled");
  else if (phase.name === "failed" && !refused) formError = rejection(phase.error);
  const idle = () => !sending && setPhase({ name: "idle" });

  return (
    <dialog
      ref={dialog}
      aria-labelledby={title}
      className="step-up-dialog bg-surface text-foreground border-border m-auto w-[calc(100%-2rem)] max-w-sm rounded-[14px] border p-0 backdrop:bg-black/40"
      onClose={onCancel}
    >
      <div className="flex flex-col gap-4 p-4 sm:p-5">
        <h2 id={title} className="text-lg font-semibold">
          {t("title")}
        </h2>
        {noFactor ? (
          <>
            <p role="status">{t("noFactor")}</p>
            <Link
              href={SECURITY}
              className={`${LINK} inline-flex min-h-11 items-center`}
              onClick={onCancel}
            >
              {t("enable")}
            </Link>
            <Button variant="ghost" className="min-h-11" onClick={() => dialog.current?.close()}>
              {t("close")}
            </Button>
          </>
        ) : (
          <form
            key={recovery ? "recovery" : "app"} // otro modo, otro campo: lo escrito no pasa
            noValidate
            method="post"
            onSubmit={submit}
            className="flex flex-col gap-4"
            aria-busy={sending}
          >
            <p className="text-muted text-sm">{t(recovery ? "introRecovery" : "intro")}</p>
            <TextField
              ref={field}
              label={t(recovery ? "recoveryCode" : "code")}
              name="code"
              type="text"
              inputMode={recovery ? "text" : "numeric"}
              autoComplete={recovery ? "off" : "one-time-code"}
              spellCheck={false}
              autoCapitalize="characters"
              maxLength={recovery ? 32 : 7}
              error={codeError}
              onInput={() => codeError && idle()}
            />
            {formError ? (
              <p role="alert" className={ALERT}>
                {formError}
              </p>
            ) : null}
            <Button
              type="submit"
              variant="primary"
              size="lg"
              aria-disabled={sending}
              className="w-full"
            >
              {t(sending ? "submitting" : "submit")}
            </Button>
            <button
              type="button"
              className={`${LINK} min-h-11 self-start text-sm`}
              onClick={() => {
                if (sending) return;
                setRecovery(!recovery);
                setPhase({ name: "idle" });
              }}
            >
              {t(recovery ? "useApp" : "useRecovery")}
            </button>
            <Button
              type="button"
              variant="ghost"
              className="min-h-11"
              onClick={() => dialog.current?.close()}
            >
              {t("cancel")}
            </Button>
          </form>
        )}
      </div>
    </dialog>
  );
}
