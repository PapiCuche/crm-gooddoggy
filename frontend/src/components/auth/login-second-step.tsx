"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthLoginMfa } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { LINK } from "./auth-card";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado
// Los `code` de esta ruta que tienen mensaje propio aquí. Cualquier otro es un fallo nuestro.
const SAID = new Set(["NETWORK_ERROR", "RATE_LIMITED", "CSRF_FAILED"]);
const NOT_STARTED = "MFA_LOGIN_NOT_STARTED"; // el acceso a medias caducó o dejó de valer

type Step =
  | { name: "idle" | "missing" | "sending" | "entered" | "stalled" }
  | { name: "failed"; error: ApiError };

// El segundo paso del acceso (ADR-023 §5): la contraseña ya se comprobó y la API espera un
// código, de la aplicación de autenticación o de respaldo. El acceso a medias vive en el
// servidor, tras la cookie de sesión; aquí no se guarda nada, tampoco el código.
// `onRestart(expired)`: vuelve al paso de la contraseña, porque la API lo pide o a voluntad.
export function LoginSecondStep({
  next,
  onRestart,
}: {
  next: string;
  onRestart: (expired: boolean) => void;
}) {
  const t = useTranslations();
  const router = useRouter();
  const queryClient = useQueryClient();
  // `always`: sin red el intento falla enseguida; no queda en cola para enviarse más tarde.
  const { mutate, reset } = useAuthLoginMfa({ mutation: { networkMode: "always" } });
  // El paso que la pantalla enseña decide si se envía. Con un código en vuelo no sale otro:
  // llegaría con la cookie anterior y, completado el acceso, la API borraría la cookie de la
  // sesión recién abierta. Tras entrar tampoco: ya no hay acceso a medias que completar.
  const [step, setStep] = useState<Step>({ name: "idle" });
  const field = useRef<HTMLInputElement>(null);

  useEffect(() => field.current?.focus(), []); // sustituye al formulario de la contraseña

  const sending = step.name === "sending";
  useEffect(() => {
    if (!sending) return;
    const timer = setTimeout(() => {
      reset(); // se deja de esperar esa respuesta: el formulario vuelve a estar disponible
      // Solo si aún se esperaba: una respuesta que llegó justo antes y aún no se pintó, manda.
      setStep((now) => (now.name === "sending" ? { name: "stalled" } : now));
    }, PATIENCE);
    return () => clearTimeout(timer);
  }, [sending, reset]);

  // Un código que no vale se dice junto al campo, que queda listo para escribir otro.
  const refused =
    step.name === "failed" &&
    step.error.code === "VALIDATION_ERROR" &&
    Object.hasOwn(step.error.fields ?? {}, "code");
  useEffect(() => {
    if (!refused) return;
    field.current?.focus();
    field.current?.select();
  }, [refused]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (step.name === "sending" || step.name === "entered") return;
    const code = String(new FormData(event.currentTarget).get("code") ?? "").trim();
    if (!code) {
      setStep({ name: "missing" });
      field.current?.focus();
      return;
    }
    setStep({ name: "sending" });
    mutate(
      { data: { code } },
      {
        onSuccess: () => {
          setStep({ name: "entered" });
          queryClient.clear(); // nada de lo que se leyó sin sesión sigue valiendo
          router.replace(next);
        },
        onError: (error) => {
          if (error.code === NOT_STARTED) onRestart(true);
          else setStep({ name: "failed", error });
        },
      },
    );
  }

  // Por `code`, nunca por el texto de la respuesta. La espera del tope de códigos llega a un día.
  function rejection(error: ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return error.retryAfter > 3600
        ? t("auth.rateLimitedHours", { hours: Math.ceil(error.retryAfter / 3600) })
        : t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    return t(`errors.api.${SAID.has(error.code) ? apiErrorKey(error) : "INTERNAL_ERROR"}`);
  }

  let fieldError: string | undefined;
  if (step.name === "missing") fieldError = t("auth.codeRequired");
  else if (refused) fieldError = t("auth.codeInvalid");
  let formError: string | null = null;
  if (step.name === "stalled") formError = t("auth.stalled");
  else if (step.name === "failed" && !refused) formError = rejection(step.error);
  const busy = sending || step.name === "entered"; // hasta que la navegación sustituye la página
  return (
    <>
      <p className="text-muted">{t("auth.secondStepIntro")}</p>
      {/* `method`: un envío antes de que cargue el JavaScript nunca pone el código en la URL. */}
      <form
        noValidate
        method="post"
        onSubmit={submit}
        className="flex flex-col gap-4"
        aria-busy={busy}
      >
        <TextField
          ref={field}
          label={t("auth.code")}
          name="code"
          type="text"
          autoComplete="one-time-code"
          autoCapitalize="characters"
          spellCheck={false}
          maxLength={32}
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
          aria-disabled={busy}
          className="mt-1 w-full"
        >
          {busy ? t("auth.secondStepSubmitting") : t("auth.secondStepSubmit")}
        </Button>
      </form>
      <p className="text-sm">
        {/* Con un código en vuelo no se vuelve: al volver se pregunta por la sesión, y esa
            petición, con la cookie anterior, puede costar la sesión que ese código abra. */}
        <button
          type="button"
          className={LINK}
          aria-disabled={busy}
          onClick={() => busy || onRestart(false)}
        >
          {t("auth.secondStepBack")}
        </button>
      </p>
    </>
  );
}
