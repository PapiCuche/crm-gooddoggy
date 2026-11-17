"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { sessionEnded } from "@/app/providers";
import { useChannelWrite } from "@/components/channels/use-channel-write";
import { FieldsForm } from "@/components/forms/fields-form";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import type { AiProviderAccount } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

type Said = { text: string; ok?: boolean; alert?: boolean } | null;

// Sin sesión (401) se va al login, también desde una escritura que no pasa por TanStack. La
// navegación completa pasa por `browser` para que un test la observe (jsdom no la implementa).
export const browser = { assign: (url: string) => window.location.assign(url) };
export function toLogin(error: ApiError) {
  const login = sessionEnded(error, window.location);
  if (login) browser.assign(login);
  return !!login;
}

// Los errores propios de estas rutas, por `code` y nunca por el texto de la respuesta.
export function aiError(
  error: ApiError,
  t: (key: string, values?: Record<string, number>) => string,
): { field?: "api_key"; text: string } | null {
  switch (error.code) {
    case "NOT_FOUND": // otra persona la borró
    case "AI_ACCOUNT_EXISTS":
    case "AI_ACCOUNT_CONFLICT":
    case "STEP_UP_REQUIRED":
      return { text: t(error.code) };
    case "RATE_LIMITED": // el tope por persona: `Retry-After` en minutos, hacia arriba
      return {
        text: error.retryAfter
          ? t("RATE_LIMITED_WAIT", { minutes: Math.ceil(error.retryAfter / 60) })
          : t("RATE_LIMITED"),
      };
    default:
      return null;
  }
}

// Lo que se dice de un error de una acción sin formulario (probar, borrar); `null`: nada.
function useProblem() {
  const errors = useTranslations("ai.errors");
  const api = useTranslations("errors.api");
  return (error: ApiError): string | null => {
    const cancelled: boolean = isStepUpCancelled(error); // cancelar el código no es un fallo
    if (cancelled || toLogin(error)) return null;
    if (error.code === "PERMISSION_DENIED") return errors("denied");
    return aiError(error, errors)?.text ?? api(apiErrorKey(error));
  };
}

// Guardar la clave: un campo de contraseña que se vacía al enviar (`secret`). La clave
// solo vive en el cierre de la petición: nunca en el estado de React, la caché de TanStack, el
// navegador ni la URL. Si la API pide el segundo factor, `run` abre el código y repite una vez.
export function KeyForm({
  send,
  onDone,
  onFailed,
}: {
  send: (apiKey: string) => Promise<AiProviderAccount>;
  onDone: (account: AiProviderAccount) => void;
  onFailed: (error: ApiError) => void; // 404 o «ya existe»: la pantalla vuelve a leer la lista
}) {
  const t = useTranslations("ai");
  const errors = useTranslations("ai.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const back = () => setTimeout(() => trigger.current?.focus());

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t("save.title")}
          heading={<h2 className="font-medium">{t("save.title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={[
            {
              ...{ name: "api_key", max: 256, label: t("key.label"), hint: t("key.hint") },
              ...{ invalid: t("key.invalid"), required: t("key.required"), secret: true },
              literal: true,
            },
          ]}
          write={write}
          send={(values) =>
            run(
              () => send(values.api_key).then(onDone),
              () => {
                setDone(true);
                setOpen(false);
                back();
              },
              (error) => toLogin(error) || onFailed(error),
            )
          }
          taken={(error) => aiError(error, errors)}
          denied={errors("denied")}
          labels={{ submit: t("save.submit"), busy: t("save.busy"), cancel: t("cancel") }}
          onCancel={() => {
            setOpen(false);
            back();
          }}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => {
            write.reset();
            setDone(false);
            setOpen(true);
          }}
        >
          {t("save.open")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm" : "sr-only"}>
        {done ? t("save.done") : ""}
      </p>
      {stepUp}
    </div>
  );
}

// Una acción de una pulsación sobre la cuenta (probar la conexión): una petición a la vez, el
// código del segundo factor si la API lo pide, y lo que pasó dicho por su código.
export function AccountAction<T>({
  label,
  busyLabel,
  run,
  done,
  onGone,
}: {
  label: string;
  busyLabel: string;
  run: () => Promise<T>;
  done: (value: T) => Said; // lo que se dice al terminar bien
  onGone: () => void; // 404: la cuenta ya no está
}) {
  const problem = useProblem();
  const { withStepUp, dialog } = useStepUp();
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said>(null);
  const sending = useRef(false); // dos pulsaciones seguidas no son dos peticiones

  function go() {
    if (sending.current) return;
    setSaid(null);
    sending.current = true;
    setBusy(true);
    withStepUp(run)
      .then(
        (value) => setSaid(done(value)),
        (error: ApiError) => {
          if (error.status === 404) onGone();
          const text = problem(error);
          setSaid(text ? { text, alert: true } : null);
        },
      )
      .finally(() => {
        sending.current = false;
        setBusy(false);
      });
  }
  return (
    <div className="flex min-w-0 flex-col items-start gap-2">
      <Button variant="primary" className="min-h-11 sm:min-h-9" aria-disabled={busy} onClick={go}>
        {busy ? busyLabel : label}
      </Button>
      <p
        role="status"
        className={said && !said.alert ? cn("text-sm", !said.ok && "text-danger") : "sr-only"}
      >
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm">
          {said.text}
        </p>
      ) : null}
      {dialog}
    </div>
  );
}
