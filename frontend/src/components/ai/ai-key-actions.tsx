"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { sessionEnded } from "@/app/providers";
import { useChannelWrite } from "@/components/channels/use-channel-write";
import { FieldsForm } from "@/components/forms/fields-form";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import type { AiProviderAccount } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

const OUTLINE = "border-border min-h-11 border sm:min-h-9";
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
): { text: string } | null {
  switch (error.code) {
    case "NOT_FOUND": // otra persona la borró
    case "AI_ACCOUNT_EXISTS":
    case "AI_ACCOUNT_CONFLICT":
    case "STEP_UP_REQUIRED":
    case "AI_DISABLED": // probar la conexión con `AI_ENABLED` apagado: no es la clave (#1065)
      return { text: t(error.code) };
    case "RATE_LIMITED": // el tope por persona: `Retry-After` en minutos, hacia arriba; una
      // espera de más de una hora no es la de este tope y se dice sin cifra
      return {
        text:
          error.retryAfter && error.retryAfter <= 3600
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

// Guardar o rotar la clave: un campo de contraseña que se vacía al enviar (`secret`). La clave
// solo vive en el cierre de la petición: nunca en el estado de React, la caché de TanStack, el
// navegador ni la URL. Si la API pide el segundo factor, `run` abre el código y repite una vez.
export function KeyForm({
  mode,
  send,
  onDone,
  onFailed,
}: {
  mode: "save" | "rotate";
  send: (apiKey: string) => Promise<AiProviderAccount>;
  onDone: (account: AiProviderAccount) => void;
  onFailed: (error: ApiError) => void; // 404 o «ya existe»: la pantalla vuelve a leer la lista
}) {
  const t = useTranslations("ai");
  const errors = useTranslations("ai.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false); // se dice solo al rotar: guardar desmonta el formulario
  const trigger = useRef<HTMLButtonElement>(null);
  const back = () => setTimeout(() => trigger.current?.focus());
  const Heading = mode === "save" ? "h2" : "h3"; // rotar va dentro de la tarjeta de la cuenta

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t(`${mode}.title`)}
          heading={<Heading className="font-medium">{t(`${mode}.title`)}</Heading>}
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
          labels={{ submit: t(`${mode}.submit`), busy: t(`${mode}.busy`), cancel: t("cancel") }}
          onCancel={() => {
            setOpen(false);
            back();
          }}
        />
      ) : (
        <Button
          ref={trigger}
          variant={mode === "save" ? "accent" : "ghost"}
          className={mode === "save" ? "min-h-11 sm:min-h-9" : OUTLINE}
          onClick={() => {
            write.reset();
            setDone(false);
            setOpen(true);
          }}
        >
          {t(`${mode}.open`)}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      {mode === "rotate" ? (
        <p role="status" className={done ? "text-sm" : "sr-only"}>
          {done ? t("rotate.done") : ""}
        </p>
      ) : null}
      {stepUp}
    </div>
  );
}

const MODEL_ID = /^[a-z0-9][a-z0-9._-]*$/; // la forma que la base exige al modelo de la cuenta

// Elegir el modelo de la cuenta (F8-12a). La API no publica la lista permitida (`AI_MODELS`): se
// escribe el identificador y la API decide; fuera de la lista vuelve un 400 en `model`, que se dice
// con nuestro texto. El mismo modelo no se envía (no cambia nada y gastaría el tope).
export function ModelForm({
  model,
  send,
  onDone,
  onGone,
}: {
  model: string;
  send: (model: string) => Promise<AiProviderAccount>;
  onDone: (account: AiProviderAccount) => void;
  onGone: () => void; // 404: la cuenta ya no está (la página lo dice)
}) {
  const t = useTranslations("ai.model");
  const ai = useTranslations("ai");
  const errors = useTranslations("ai.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const close = (text: string | null) => {
    setSaid(text);
    setOpen(false);
    setTimeout(() => trigger.current?.focus());
  };

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h3 className="font-medium">{t("title")}</h3>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={[
            {
              ...{ name: "model", max: 64, label: t("label"), hint: t("hint") },
              ...{ invalid: t("invalid"), required: t("required"), literal: true },
              ...{ defaultValue: model, valid: (value: string) => MODEL_ID.test(value) },
            },
          ]}
          write={write}
          send={(values) =>
            values.model === model
              ? close(t("unchanged"))
              : run(
                  () => send(values.model).then(onDone),
                  () => close(t("done")),
                  (error) => toLogin(error) || (error.status === 404 && onGone()),
                )
          }
          taken={(error) => aiError(error, errors)}
          denied={errors("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: ai("cancel") }}
          onCancel={() => close(null)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className={OUTLINE}
          onClick={() => {
            write.reset();
            setSaid(null);
            setOpen(true);
          }}
        >
          {t("open")}
        </Button>
      )}
      <p role="status" className={said ? "text-sm" : "sr-only"}>
        {said ?? ""}
      </p>
      {stepUp}
    </div>
  );
}

// Una acción de una pulsación sobre la cuenta (probar la conexión, borrar): una petición a la vez,
// el código del segundo factor si la API lo pide, y lo que pasó dicho por su código. Con
// `confirm`, antes pregunta lo que pasa, con el foco en «Cancelar» (lo seguro).
export function AccountAction<T>({
  label,
  busyLabel,
  run,
  done,
  onGone,
  confirm,
}: {
  label: string;
  busyLabel: string;
  run: () => Promise<T>;
  done: (value: T) => Said; // lo que se dice al terminar bien
  onGone: () => void; // 404: la cuenta ya no está (la página lo dice)
  confirm?: { title: string; question: string; effect: string; yes: string; no: string };
}) {
  const problem = useProblem();
  const { withStepUp, dialog } = useStepUp();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said>(null);
  const sending = useRef(false); // dos pulsaciones seguidas no son dos peticiones
  const cancel = useRef<HTMLButtonElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (asking) cancel.current?.focus();
  }, [asking]);

  function go() {
    if (sending.current) return;
    setSaid(null);
    if (confirm && !asking) return setAsking(true);
    sending.current = true;
    setBusy(true);
    withStepUp(run)
      .then(
        (value) => {
          setAsking(false);
          setSaid(done(value));
        },
        (error: ApiError) => {
          if (error.status === 404) return onGone(); // lo dice la página: esta acción se va
          const text = problem(error);
          setSaid(text ? { text, alert: true } : null);
        },
      )
      .finally(() => {
        sending.current = false;
        setBusy(false);
      });
  }
  const press = (
    <Button
      ref={asking ? undefined : trigger}
      variant={confirm && !asking ? "ghost" : "primary"}
      className={confirm && !asking ? OUTLINE : "min-h-11 sm:min-h-9"}
      aria-disabled={busy}
      onClick={go}
    >
      {busy ? busyLabel : asking ? confirm?.yes : label}
    </Button>
  );

  return (
    <div className="flex min-w-0 flex-col items-start gap-2">
      {asking && confirm ? (
        <div
          role="group"
          aria-label={confirm.title}
          className="border-danger flex w-full max-w-xl flex-col gap-3 rounded-lg border p-3"
        >
          <p>{confirm.question}</p>
          <p className="text-muted text-sm">{confirm.effect}</p>
          <div className="flex flex-wrap gap-2">
            <Button
              ref={cancel}
              variant="ghost"
              className={OUTLINE}
              aria-disabled={busy}
              onClick={() => {
                if (sending.current) return;
                setAsking(false);
                setTimeout(() => trigger.current?.focus());
              }}
            >
              {confirm.no}
            </Button>
            {press}
          </div>
        </div>
      ) : (
        press
      )}
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
