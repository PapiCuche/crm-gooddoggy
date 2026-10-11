"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthPasswordResetRequest } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado

type Step =
  | { name: "idle" | "missing" | "sending" | "sent" | "stalled" }
  | { name: "failed"; error: ApiError };

// Pide un enlace de recuperación (ADR-021 §2). La API responde lo mismo exista o no la cuenta,
// y la pantalla también: un único mensaje para cualquier correo admitido, que no repite lo
// escrito. El correo solo viaja en el cuerpo de la petición; aquí no se guarda nada.
export function PasswordResetRequest() {
  const t = useTranslations();
  // `always`: sin red la petición falla enseguida; no queda en cola para enviarse más tarde.
  const { mutate, reset } = useAuthPasswordResetRequest({ mutation: { networkMode: "always" } });
  // El paso que la pantalla enseña decide si se envía: una pulsación mantenida no envía otra
  // petición entre la respuesta y el momento en que la pantalla la enseña.
  const [step, setStep] = useState<Step>({ name: "idle" });
  const field = useRef<HTMLInputElement>(null);
  const sent = useRef<HTMLHeadingElement>(null);

  const sending = step.name === "sending";
  useEffect(() => {
    if (!sending) return;
    const timer = setTimeout(() => {
      reset(); // se deja de esperar esa respuesta: el formulario vuelve a estar disponible
      setStep({ name: "stalled" });
    }, PATIENCE);
    return () => clearTimeout(timer);
  }, [sending, reset]);

  // El formulario desaparece al enviar: el foco va a lo que lo sustituye.
  const done = step.name === "sent";
  useEffect(() => {
    if (done) sent.current?.focus();
  }, [done]);

  // Lo que la API no acepta del campo se dice junto al campo.
  const refused =
    step.name === "failed" &&
    step.error.code === "VALIDATION_ERROR" &&
    Object.hasOwn(step.error.fields ?? {}, "email");
  useEffect(() => {
    if (refused) field.current?.focus();
  }, [refused]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (step.name === "sending" || step.name === "sent") return;
    const email = String(new FormData(event.currentTarget).get("email") ?? "").trim();
    if (!email) {
      setStep({ name: "missing" });
      field.current?.focus();
      return;
    }
    setStep({ name: "sending" });
    mutate(
      { data: { email } },
      {
        onSuccess: () => setStep({ name: "sent" }),
        onError: (error) => setStep({ name: "failed", error }),
      },
    );
  }

  // Por `code`, nunca por el texto de la respuesta. Ninguno dice nada de ninguna cuenta.
  function rejection(error: ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    // Un 400 que no es del correo no es algo que la persona pueda corregir.
    return t(
      `errors.api.${error.code === "VALIDATION_ERROR" ? "INTERNAL_ERROR" : apiErrorKey(error)}`,
    );
  }

  if (done) {
    return (
      <div className="flex flex-col gap-3">
        <h2 ref={sent} tabIndex={-1} className="text-lg font-semibold focus-visible:outline-none">
          {t("recovery.sentTitle")}
        </h2>
        <p>{t("recovery.sentBody")}</p>
        <p className="text-muted text-sm">{t("recovery.sentHint")}</p>
      </div>
    );
  }

  let fieldError: string | undefined;
  if (step.name === "missing") fieldError = t("auth.emailRequired");
  else if (refused) fieldError = t("recovery.emailInvalid");
  let formError: string | null = null;
  if (step.name === "stalled") formError = t("auth.stalled");
  else if (step.name === "failed" && !refused) formError = rejection(step.error);
  return (
    <>
      <p className="text-muted">{t("recovery.intro")}</p>
      {/* `method`: un envío antes de que cargue el JavaScript nunca pone el correo en la URL. */}
      <form
        noValidate
        method="post"
        onSubmit={submit}
        className="flex flex-col gap-4"
        aria-busy={sending}
      >
        <TextField
          ref={field}
          label={t("auth.email")}
          name="email"
          type="email"
          autoComplete="email"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={254}
          error={fieldError}
          // Al corregir, el aviso anterior ya no describe lo escrito.
          onInput={() => fieldError && setStep({ name: "idle" })}
        />
        {formError ? (
          <p
            role="alert"
            className="border-danger/40 text-danger rounded-[10px] border px-3 py-2 text-sm transition-[opacity,transform] duration-200 ease-(--ease-out) starting:translate-y-1 starting:opacity-0"
          >
            {formError}
          </p>
        ) : null}
        {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
        <Button
          type="submit"
          variant="primary"
          size="lg"
          aria-disabled={sending}
          className="mt-1 w-full"
        >
          {sending ? t("recovery.submitting") : t("recovery.submit")}
        </Button>
      </form>
    </>
  );
}
