"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthMfaTotpConfirm, useAuthMfaTotpStart } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { QrCode } from "./qr-code";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado
// Los `code` de estas rutas con mensaje propio aquí. Cualquier otro es un fallo nuestro.
const SAID = new Set(["NETWORK_ERROR", "RATE_LIMITED", "CSRF_FAILED"]);
export const ALERT =
  "border-danger/40 text-danger rounded-[10px] border px-3 py-2 text-sm transition-[opacity,transform] duration-200 ease-(--ease-out) starting:translate-y-1 starting:opacity-0";

export type Phase =
  { name: "idle" | "missing" | "sending" | "stalled" } | { name: "failed"; error: ApiError };

// Deja de esperar una respuesta a los `PATIENCE` ms; si llegó justo antes y aún no se pintó, manda.
export function useStall(
  sending: boolean,
  reset: () => void,
  set: (next: (now: Phase) => Phase) => void,
) {
  useEffect(() => {
    if (!sending) return;
    const timer = setTimeout(() => {
      reset();
      set((now) => (now.name === "sending" ? { name: "stalled" } : now));
    }, PATIENCE);
    return () => clearTimeout(timer);
  }, [sending, reset, set]);
}

export function useRejection() {
  const t = useTranslations();
  return (error: ApiError) => {
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return error.retryAfter > 3600
        ? t("auth.rateLimitedHours", { hours: Math.ceil(error.retryAfter / 3600) })
        : t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    return t(`errors.api.${SAID.has(error.code) ? apiErrorKey(error) : "INTERNAL_ERROR"}`);
  };
}

export const refusedField = (phase: Phase, field: string) =>
  phase.name === "failed" &&
  phase.error.code === "VALIDATION_ERROR" &&
  Object.hasOwn(phase.error.fields ?? {}, field);

type Step =
  | { name: "password"; expired: boolean }
  | { name: "code"; secret: string; uri: string }
  | { name: "codes"; codes: string[] };

// El alta del segundo factor (ADR-023 §4), en tres pasos: la contraseña, la clave y un código
// de la aplicación, y los códigos de respaldo. La clave y los códigos se enseñan una vez: no se
// guardan en el navegador, y las peticiones se olvidan al terminar (`gcTime: 0`).
export function MfaEnrollment({
  onFinished,
  onCancel,
}: {
  onFinished: () => void;
  onCancel: () => void;
}) {
  const [step, setStep] = useState<Step>({ name: "password", expired: false });
  if (step.name === "password") {
    return (
      <PasswordStep
        expired={step.expired}
        onStarted={(secret, uri) => setStep({ name: "code", secret, uri })}
        onAlready={onFinished}
        onCancel={onCancel}
      />
    );
  }
  if (step.name === "code") {
    return (
      <CodeStep
        secret={step.secret}
        uri={step.uri}
        onConfirmed={(codes) => setStep({ name: "codes", codes })}
        onExpired={() => setStep({ name: "password", expired: true })}
        onAlready={onFinished}
        onCancel={onCancel}
      />
    );
  }
  return <CodesStep codes={step.codes} onDone={onFinished} sessionsClosed />;
}

function PasswordStep({
  expired,
  onStarted,
  onAlready,
  onCancel,
}: {
  expired: boolean;
  onStarted: (secret: string, uri: string) => void;
  onAlready: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations();
  const rejection = useRejection();
  const { mutate, reset } = useAuthMfaTotpStart({
    mutation: { networkMode: "always", gcTime: 0 },
  });
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => field.current?.focus(), []);
  useStall(phase.name === "sending", reset, setPhase);
  const refused = refusedField(phase, "password");
  useEffect(() => {
    if (refused) field.current?.focus();
  }, [refused]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase.name === "sending") return;
    const password = String(new FormData(event.currentTarget).get("password") ?? ""); // tal cual
    if (!password) {
      setPhase({ name: "missing" });
      field.current?.focus();
      return;
    }
    setPhase({ name: "sending" });
    mutate(
      { data: { password } },
      {
        onSuccess: (enrollment) => onStarted(enrollment.secret, enrollment.uri),
        onError: (error) => {
          if (error.code === "MFA_ALREADY_ENABLED") onAlready();
          else setPhase({ name: "failed", error });
        },
      },
    );
  }

  let fieldError: string | undefined;
  if (phase.name === "missing") fieldError = t("account.passwordRequired");
  else if (refused) fieldError = t("account.passwordInvalid");
  let formError: string | null = null;
  if (phase.name === "stalled") formError = t("auth.stalled");
  else if (phase.name === "failed" && !refused) formError = rejection(phase.error);
  else if (expired && phase.name === "idle") formError = t("account.expired");
  const sending = phase.name === "sending";
  return (
    <form
      noValidate
      method="post"
      onSubmit={submit}
      className="flex flex-col gap-4"
      aria-busy={sending}
    >
      <p className="text-muted">{t("account.stepPassword")}</p>
      <TextField
        ref={field}
        label={t("account.password")}
        name="password"
        type="password"
        autoComplete="current-password"
        maxLength={1024}
        error={fieldError}
        onInput={() => fieldError && setPhase({ name: "idle" })}
      />
      {formError ? (
        <p role="alert" className={ALERT}>
          {formError}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" aria-disabled={sending} className="w-full">
        {sending ? t("account.continuing") : t("account.continue")}
      </Button>
      <Button type="button" variant="ghost" onClick={onCancel}>
        {t("account.cancel")}
      </Button>
    </form>
  );
}

function CodeStep({
  secret,
  uri,
  onConfirmed,
  onExpired,
  onAlready,
  onCancel,
}: {
  secret: string;
  uri: string;
  onConfirmed: (codes: string[]) => void;
  onExpired: () => void;
  onAlready: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations();
  const rejection = useRejection();
  const { mutate, reset } = useAuthMfaTotpConfirm({
    mutation: { networkMode: "always", gcTime: 0 },
  });
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const [copied, setCopied] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  const intro = useRef<HTMLParagraphElement>(null);
  useEffect(() => intro.current?.focus(), []); // sustituye al formulario: se lee antes la clave
  useStall(phase.name === "sending", reset, setPhase);
  const refused = refusedField(phase, "code");
  useEffect(() => {
    if (!refused) return;
    field.current?.focus();
    field.current?.select();
  }, [refused]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase.name === "sending") return;
    // Como lo enseñan las aplicaciones (`123 456`): esta ruta no lo junta y lo contaría como fallo.
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
        onSuccess: (answer) => onConfirmed(answer.backup_codes),
        onError: (error) => {
          if (error.code === "MFA_ENROLLMENT_NOT_STARTED") onExpired();
          else if (error.code === "MFA_ALREADY_ENABLED") onAlready();
          else setPhase({ name: "failed", error });
        },
      },
    );
  }

  let fieldError: string | undefined;
  if (phase.name === "missing") fieldError = t("account.codeRequired");
  else if (refused) fieldError = t("account.codeInvalid");
  let formError: string | null = null;
  if (phase.name === "stalled") formError = t("auth.stalled");
  else if (phase.name === "failed" && !refused) formError = rejection(phase.error);
  const sending = phase.name === "sending";
  return (
    <form
      noValidate
      method="post"
      onSubmit={submit}
      className="flex flex-col gap-4"
      aria-busy={sending}
    >
      <p ref={intro} tabIndex={-1} className="text-muted">
        {t("account.stepKey")}
      </p>
      <QrCode value={uri} label={t("account.qrLabel")} />
      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium" id="mfa-key-label">
          {t("account.keyLabel")}
        </span>
        <code
          aria-labelledby="mfa-key-label"
          className="bg-surface-raised rounded-[10px] px-3 py-2 font-mono text-[16px] tracking-wider wrap-anywhere"
        >
          {secret.match(/.{1,4}/g)?.join(" ")}
        </code>
        <div className="flex flex-wrap gap-2">
          <Button
            type="button"
            variant="accent"
            onClick={() =>
              void navigator.clipboard?.writeText(secret).then(
                () => setCopied(true),
                () => setCopied(false),
              )
            }
          >
            {copied ? t("account.copied") : t("account.copyKey")}
          </Button>
          <a
            href={uri}
            className="inline-flex min-h-11 items-center text-sm underline underline-offset-4"
          >
            {t("account.openApp")}
          </a>
        </div>
      </div>
      <p className="text-muted">{t("account.stepCode")}</p>
      <TextField
        ref={field}
        label={t("account.code")}
        name="code"
        type="text"
        inputMode="numeric"
        autoComplete="one-time-code"
        spellCheck={false}
        maxLength={16}
        error={fieldError}
        onInput={() => fieldError && setPhase({ name: "idle" })}
      />
      {formError ? (
        <p role="alert" className={ALERT}>
          {formError}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" aria-disabled={sending} className="w-full">
        {sending ? t("account.confirming") : t("account.confirm")}
      </Button>
      <Button
        type="button"
        variant="ghost"
        aria-disabled={sending}
        onClick={() => sending || onCancel()}
      >
        {t("account.cancel")}
      </Button>
    </form>
  );
}

// Los códigos de respaldo recién hechos, una sola vez (el alta y la regeneración).
export function CodesStep({
  codes,
  onDone,
  sessionsClosed = false,
}: {
  codes: string[];
  onDone: () => void;
  sessionsClosed?: boolean;
}) {
  const t = useTranslations();
  const [saved, setSaved] = useState(false);
  const [copied, setCopied] = useState(false);
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => title.current?.focus(), []); // sustituye al formulario
  const text = `${t("account.codesFile")}\n\n${codes.join("\n")}\n`;

  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = "good-doggy-codigos-de-respaldo.txt";
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="flex flex-col gap-4">
      <h3 ref={title} tabIndex={-1} className="font-semibold focus-visible:outline-none">
        {t("account.codesTitle")}
      </h3>
      <p className="text-muted">{t("account.codesIntro")}</p>
      <ol className="bg-surface-raised grid grid-cols-1 gap-1 rounded-[10px] px-3 py-2 font-mono text-[16px] sm:grid-cols-2">
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="accent"
          onClick={() =>
            void navigator.clipboard?.writeText(codes.join("\n")).then(
              () => setCopied(true),
              () => setCopied(false),
            )
          }
        >
          {copied ? t("account.copied") : t("account.copyCodes")}
        </Button>
        <Button type="button" variant="accent" onClick={download}>
          {t("account.downloadCodes")}
        </Button>
      </div>
      {sessionsClosed ? <p className="text-muted text-sm">{t("account.sessionsClosed")}</p> : null}
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={saved}
          onChange={(event) => setSaved(event.currentTarget.checked)}
          className="mt-0.5 size-4"
        />
        {t("account.saved")}
      </label>
      <Button
        type="button"
        variant="primary"
        size="lg"
        aria-disabled={!saved}
        className="w-full"
        onClick={() => saved && onDone()}
      >
        {t("account.finish")}
      </Button>
    </div>
  );
}
