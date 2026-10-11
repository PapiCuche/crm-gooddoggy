"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { LINK } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthPasswordResetConfirm } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { text } from "@/lib/message-text";

import messages from "../../../messages/es-PE.json";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado
// La forma del secreto que emite la API (`apps.accounts.recovery.SECRET`).
const SECRET = /^[A-Za-z0-9_-]{43}$/;
// Los `code` de esta ruta que tienen mensaje propio aquí. Cualquier otro es un fallo nuestro.
// También `CSRF_FAILED`: su texto pide recargar, y recargar esta página pierde el enlace.
const SAID = new Set(["NETWORK_ERROR", "RATE_LIMITED"]);
const REASONS: unknown = messages.recovery.reasons;

type Step =
  | { name: "opening" | "idle" | "missing" | "sending" | "stalled" | "changed" }
  // `absent`: la página se abrió sin enlace. `unsure`: la API lo rechazó después de un intento
  // que quedó sin respuesta, y que por tanto pudo cambiar la contraseña.
  | { name: "invalid"; why: "absent" | "refused" | "unsure" }
  | { name: "failed"; error: ApiError };

// Lo que los validadores de la API dicen de la contraseña, por su `code` y nunca por el texto
// de la respuesta. Un código sin texto propio aquí usa el general.
function reasons(error: ApiError): string[] | null {
  if (error.code !== "VALIDATION_ERROR" || !Object.hasOwn(error.fields ?? {}, "password")) {
    return null;
  }
  const given: unknown = error.fields?.password;
  const known = (Array.isArray(given) ? given : [])
    .map((item: unknown) => text(REASONS, String((item as { code?: unknown } | null)?.code)))
    .filter((reason) => reason !== null);
  return [...new Set(known)];
}

// Cambia la contraseña con el enlace de recuperación (ADR-021 §4 y §5). El secreto llega en el
// fragmento de la URL, que el navegador no envía al servidor: aquí se saca de la barra de
// direcciones nada más leerlo, se guarda solo en memoria y viaja solo en el cuerpo de la
// petición. No llega al DOM ni al almacenamiento del navegador.
export function PasswordResetChange() {
  const t = useTranslations();
  // `always`: sin red falla enseguida. `gcTime`: la caché de escrituras no retiene el secreto
  // ni la contraseña de una petición terminada que ya nadie mira. No 0: mientras una petición
  // abandonada siga pendiente la caché los retiene, y reprogramaría su limpieza sin pausa.
  const { mutate, reset } = useAuthPasswordResetConfirm({
    mutation: { networkMode: "always", gcTime: 250 },
  });
  // El paso que la pantalla enseña decide si se envía: una pulsación mantenida no envía otra
  // petición entre la respuesta y el momento en que la pantalla la enseña.
  const [step, setStep] = useState<Step>({ name: "opening" });
  const secret = useRef<string | null>(null);
  const opened = useRef(false);
  const unsure = useRef(false);
  const field = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  // Lee el enlace y lo quita de la barra de direcciones y de su entrada del historial.
  const open = useCallback(() => {
    const given = window.location.hash.slice(1);
    const { pathname, search } = window.location;
    const clean = pathname + search;
    window.history.replaceState(window.history.state, "", clean);
    // El router de Next guarda su propia copia de la URL, con el fragmento, y la volvería a
    // escribir en la barra en su siguiente actualización (comprobado en un navegador). Se entera
    // por un `replaceState` sin su marca en el estado, que él completa; y no antes de que este
    // efecto termine: al cargar la página, Next aún no ha puesto su `replaceState`.
    queueMicrotask(() => window.history.replaceState(null, "", clean));
    reset(); // lo que quedara en vuelo con el enlace anterior ya no cambia esta pantalla
    secret.current = SECRET.test(given) ? given : null;
    unsure.current = false;
    if (field.current) field.current.value = ""; // lo escrito era para el enlace anterior
    if (secret.current) setStep({ name: "idle" });
    else setStep({ name: "invalid", why: given ? "refused" : "absent" });
  }, [reset]);
  useEffect(() => {
    if (!opened.current) {
      opened.current = true;
      open();
    }
    // Otro enlace abierto en esta misma pestaña solo cambia el fragmento: la página no se
    // vuelve a cargar, y el secreto nuevo tampoco se queda en la barra de direcciones.
    const another = () => {
      if (window.location.hash) open();
    };
    window.addEventListener("hashchange", another);
    return () => window.removeEventListener("hashchange", another);
  }, [open]);

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
  // Sin respuesta, esa petición pudo llegar y cambiar la contraseña.
  const stalled = step.name === "stalled";
  useEffect(() => {
    if (stalled) unsure.current = true;
  }, [stalled]);

  // Al abrirse el formulario el foco va a la contraseña; cuando deja de ofrecerse, a lo que
  // lo sustituye.
  const shown = step.name === "invalid" || step.name === "changed" ? step.name : "form";
  const ready = step.name !== "opening";
  useEffect(() => {
    if (!ready) return;
    if (shown !== "form") heading.current?.focus();
    else if (document.activeElement === document.body) field.current?.focus();
  }, [ready, shown]);

  // Lo que la API no acepta de la contraseña se dice junto al campo, con el foco en él.
  const refused = step.name === "failed" ? reasons(step.error) : null;
  const refusal = refused ? step : null; // la respuesta, no cada render
  useEffect(() => {
    if (refusal) field.current?.focus();
  }, [refusal]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = secret.current;
    if (step.name === "sending" || !token) return;
    // Tal cual: los espacios cuentan.
    const password = String(new FormData(event.currentTarget).get("password") ?? "");
    if (!password) {
      setStep({ name: "missing" });
      field.current?.focus();
      return;
    }
    setStep({ name: "sending" });
    mutate(
      { data: { token, password } },
      {
        onSuccess: () => {
          secret.current = null; // ya está usado
          setStep({ name: "changed" });
        },
        onError: (error) => {
          if (error.code === "INVALID_RESET_LINK") {
            secret.current = null; // no vale: no se vuelve a presentar
            setStep({ name: "invalid", why: unsure.current ? "unsure" : "refused" });
            return;
          }
          if (error.code === "NETWORK_ERROR") unsure.current = true; // pudo llegar igualmente
          setStep({ name: "failed", error });
        },
        // Lo que hay que saber de la respuesta ya está en `step`: soltar la escritura deja
        // que la caché la borre (`gcTime`), con el secreto y la contraseña que llevaba.
        onSettled: () => reset(),
      },
    );
  }

  // Por `code`. El texto general de otros códigos engañaría aquí (el de un 404 no es «el
  // enlace no existe»): lo que esta ruta no contesta se dice como un fallo del servidor.
  function rejection(error: ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    return t(`errors.api.${SAID.has(error.code) ? apiErrorKey(error) : "INTERNAL_ERROR"}`);
  }

  if (step.name === "opening") return <p className="text-muted">{t("recovery.opening")}</p>;
  if (step.name === "invalid" || step.name === "changed") {
    const changed = step.name === "changed";
    const absent = step.name === "invalid" && step.why === "absent";
    const title = absent ? "recovery.absentTitle" : "recovery.invalidTitle";
    const body = absent ? "recovery.absentBody" : "recovery.invalidBody";
    return (
      <div className="flex flex-col gap-3">
        <h2
          ref={heading}
          tabIndex={-1}
          className="text-lg font-semibold focus-visible:outline-none"
        >
          {t(changed ? "recovery.changedTitle" : title)}
        </h2>
        <p>{t(changed ? "recovery.changedBody" : body)}</p>
        {step.name === "invalid" && step.why === "unsure" ? (
          <>
            <p>{t("recovery.invalidUnsure")}</p>
            <p className="text-sm">
              <Link href="/login" className={LINK}>
                {t("recovery.enter")}
              </Link>
            </p>
          </>
        ) : null}
        <p className="text-sm">
          <Link href={changed ? "/login" : "/recuperar"} className={LINK}>
            {t(changed ? "recovery.enter" : "recovery.askAgain")}
          </Link>
        </p>
      </div>
    );
  }

  let fieldError: string | undefined;
  if (step.name === "missing") fieldError = t("recovery.passwordRequired");
  else if (refused) fieldError = refused.join(" ") || t("recovery.passwordRefused");
  let formError: string | null = null;
  if (step.name === "stalled") formError = t("auth.stalled");
  else if (step.name === "failed" && !refused) formError = rejection(step.error);
  return (
    <>
      <p className="text-muted">{t("recovery.changeIntro")}</p>
      {/* `method`: un envío antes de que cargue el JavaScript nunca pone la contraseña en la URL. */}
      <form
        noValidate
        method="post"
        onSubmit={submit}
        className="flex flex-col gap-4"
        aria-busy={sending}
      >
        <TextField
          ref={field}
          label={t("recovery.newPassword")}
          name="password"
          type="password"
          autoComplete="new-password"
          maxLength={1024}
          hint={t("recovery.passwordHint")}
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
          {sending ? t("recovery.changing") : t("recovery.change")}
        </Button>
      </form>
    </>
  );
}
