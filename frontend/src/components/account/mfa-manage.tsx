"use client";

import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthMfaBackupCodesRegenerate, useAuthMfaDisable } from "@/lib/api/client";
import type { MfaBackupCodes } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

import {
  ALERT,
  CodesStep,
  type Phase,
  refusedField,
  useRejection,
  useStall,
} from "./mfa-enrollment";

const NOT_ENABLED = "MFA_NOT_ENABLED";

type Send<T> = (
  data: { password: string; code: string },
  on: { onSuccess: (answer: T) => void; onError: (error: ApiError) => void },
) => void;

// La contraseña actual y un código, para un cambio del segundo factor (ADR-023 §6 y §8). Un
// envío por pulsación; la contraseña va tal cual y el código sin espacios ni guiones. El foco
// empieza en la contraseña: `warning`, el id de una advertencia de `intro`, la describe para
// que un lector de pantalla no se la salte.
function PasswordAndCode<T>({
  intro,
  warning,
  codeHint,
  action,
  acting,
  send,
  reset,
  onDone,
  onNotEnabled,
  onCancel,
}: {
  intro: ReactNode;
  warning?: string;
  codeHint: string;
  action: string;
  acting: string;
  send: Send<T>;
  reset: () => void;
  onDone: (answer: T) => void;
  onNotEnabled: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations();
  const rejection = useRejection();
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const [missing, setMissing] = useState<"password" | "code" | null>(null);
  const password = useRef<HTMLInputElement>(null);
  const code = useRef<HTMLInputElement>(null);
  useEffect(() => password.current?.focus(), []);
  useStall(phase.name === "sending", reset, setPhase);
  const badPassword = refusedField(phase, "password");
  const badCode = refusedField(phase, "code");
  useEffect(() => {
    if (badPassword) password.current?.focus();
    else if (badCode) {
      code.current?.focus();
      code.current?.select();
    }
  }, [badPassword, badCode]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (phase.name === "sending") return;
    const form = new FormData(event.currentTarget);
    const typed = String(form.get("password") ?? ""); // tal cual
    const shown = String(form.get("code") ?? "").replace(/[\s-]/g, "");
    const empty = !typed ? "password" : !shown ? "code" : null;
    if (empty) {
      setMissing(empty);
      setPhase({ name: "missing" });
      (empty === "password" ? password : code).current?.focus();
      return;
    }
    setMissing(null);
    setPhase({ name: "sending" });
    send(
      { password: typed, code: shown },
      {
        onSuccess: onDone,
        onError: (error) => {
          if (error.code === NOT_ENABLED) onNotEnabled();
          else setPhase({ name: "failed", error });
        },
      },
    );
  }

  const idle = () => phase.name !== "sending" && setPhase({ name: "idle" });
  let passwordError: string | undefined;
  if (missing === "password" && phase.name === "missing") {
    passwordError = t("account.passwordRequired");
  } else if (badPassword) passwordError = t("account.passwordInvalid");
  let codeError: string | undefined;
  if (missing === "code" && phase.name === "missing") codeError = t("account.codeRequired");
  else if (badCode) codeError = codeHint;
  let formError: string | null = null;
  if (phase.name === "stalled") formError = t("auth.stalled");
  else if (phase.name === "failed" && !badPassword && !badCode) formError = rejection(phase.error);
  const sending = phase.name === "sending";
  return (
    <form
      noValidate
      method="post"
      onSubmit={submit}
      className="flex flex-col gap-4"
      aria-busy={sending}
    >
      {intro}
      <TextField
        ref={password}
        label={t("account.password")}
        name="password"
        type="password"
        autoComplete="current-password"
        maxLength={1024}
        aria-describedby={warning}
        error={passwordError}
        onInput={() => passwordError && idle()}
      />
      <TextField
        ref={code}
        label={t("account.code")}
        name="code"
        type="text"
        autoComplete="one-time-code"
        spellCheck={false}
        maxLength={32}
        error={codeError}
        onInput={() => codeError && idle()}
      />
      {formError ? (
        <p role="alert" className={ALERT}>
          {formError}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" aria-disabled={sending} className="w-full">
        {sending ? acting : action}
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

// Diez códigos de respaldo nuevos en lugar de los que queden (ADR-023 §6): solo con un código
// de la aplicación. Se enseñan una vez y la petición se olvida al terminar (`gcTime: 0`).
export function MfaRegenerate({
  onFinished,
  onCancel,
}: {
  onFinished: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations();
  const { mutate, reset } = useAuthMfaBackupCodesRegenerate({
    mutation: { networkMode: "always", gcTime: 0 },
  });
  const [codes, setCodes] = useState<string[] | null>(null);
  if (codes) return <CodesStep codes={codes} onDone={onFinished} />;
  return (
    <PasswordAndCode<MfaBackupCodes>
      intro={<p className="text-muted">{t("account.regenerateIntro")}</p>}
      codeHint={t("account.regenerateCodeInvalid")}
      action={t("account.regenerate")}
      acting={t("account.regenerating")}
      send={(data, on) => mutate({ data }, on)}
      reset={reset}
      onDone={(answer) => {
        reset(); // la contraseña y el código no quedan en la caché mientras se ven los códigos
        setCodes(answer.backup_codes);
      }}
      onNotEnabled={onFinished}
      onCancel={onCancel}
    />
  );
}

// Quitar el segundo factor (ADR-023 §8): con un código de la aplicación o uno de respaldo. La
// API cierra además las demás sesiones; esta sigue.
export function MfaDisable({
  onDisabled,
  onNotEnabled,
  onCancel,
}: {
  onDisabled: () => void;
  onNotEnabled: () => void;
  onCancel: () => void;
}) {
  const t = useTranslations();
  const { mutate, reset } = useAuthMfaDisable({ mutation: { networkMode: "always", gcTime: 0 } });
  const warning = useId();
  return (
    <PasswordAndCode<void>
      intro={
        <>
          <p
            id={warning}
            role="note"
            className="border-danger/40 rounded-[10px] border px-3 py-2 text-sm"
          >
            {t("account.disableWarning")}
          </p>
          <p className="text-muted">{t("account.disableIntro")}</p>
        </>
      }
      warning={warning}
      codeHint={t("account.disableCodeInvalid")}
      action={t("account.disable")}
      acting={t("account.disabling")}
      send={(data, on) => mutate({ data }, on)}
      reset={reset}
      onDone={onDisabled}
      onNotEnabled={onNotEnabled}
      onCancel={onCancel}
    />
  );
}
