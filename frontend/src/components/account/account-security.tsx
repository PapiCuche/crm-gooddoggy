"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { SessionActions } from "@/components/app-shell/session-actions";
import { LINK } from "@/components/auth/auth-card";
import { Button } from "@/components/ui/button";
import { useAuthMfaStatus } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import { ApiError } from "@/lib/http";

import { MfaEnrollment } from "./mfa-enrollment";
import { MfaDisable, MfaRegenerate } from "./mfa-manage";

const FEW = 3; // códigos de respaldo a partir de los que se avisa

type Mode = "idle" | "enrolling" | "regenerating" | "disabling";

// La seguridad de la cuenta propia (ADR-023): el estado del segundo factor, su alta, la
// regeneración de los códigos de respaldo y quitarlo. Lo que dice viene de la API
// (`GET /auth/mfa/`); la pantalla no decide nada.
export function AccountSecurity() {
  const t = useTranslations();
  const format = useFormatter();
  // Con un cambio en curso no se vuelve a preguntar solo: cambiaría la pantalla bajo los pasos.
  const status = useAuthMfaStatus({
    query: { refetchOnWindowFocus: false, refetchOnReconnect: false },
  });
  const [mode, setMode] = useState<Mode>("idle");
  const [disabled, setDisabled] = useState(false); // se acaba de quitar desde esta pantalla
  const busy = mode !== "idle";
  // Al quitarlo, el formulario desaparece: el foco va al aviso, que así también se lee.
  const notice = useRef<HTMLParagraphElement>(null);
  const shown = disabled && status.isSuccess && !status.isRefetching;
  useEffect(() => {
    if (shown) notice.current?.focus();
  }, [shown]);
  const back = () => {
    setMode("idle");
    void status.refetch();
  };
  // Sin sesión, `Providers` ya lleva al login: aquí solo se evita mostrar un error mientras.
  const expired = status.error instanceof ApiError && status.error.status === 401;

  let body;
  if (status.isPending || status.isRefetching || expired) {
    body = <p className="text-muted">{t("account.loading")}</p>;
  } else if (status.isError) {
    body = (
      <>
        <p role="alert" className="text-danger">
          {t(`errors.api.${apiErrorKey(status.error)}`)}
        </p>
        <Button variant="primary" size="lg" onClick={() => void status.refetch()}>
          {t("account.retry")}
        </Button>
      </>
    );
  } else if (mode === "enrolling") {
    body = <MfaEnrollment onFinished={back} onCancel={() => setMode("idle")} />;
  } else if (mode === "regenerating") {
    body = <MfaRegenerate onFinished={back} onCancel={() => setMode("idle")} />;
  } else if (mode === "disabling") {
    body = (
      <MfaDisable
        onDisabled={() => {
          setDisabled(true);
          back();
        }}
        onNotEnabled={back}
        onCancel={() => setMode("idle")}
      />
    );
  } else if (status.data.enabled) {
    const left = status.data.backup_codes_left;
    const since = status.data.enabled_at ? new Date(status.data.enabled_at) : null;
    body = (
      <>
        <p>
          {since
            ? t("account.mfaOn", { date: format.dateTime(since, { dateStyle: "long" }) })
            : t("account.mfaOnUndated")}
        </p>
        <p className={left <= FEW ? "text-danger" : "text-muted"}>
          {t("account.backupLeft", { count: left })}
          {left <= FEW ? ` ${t("account.backupFew")}` : null}
        </p>
        <div className="flex flex-col gap-2">
          <Button variant="accent" size="lg" onClick={() => setMode("regenerating")}>
            {t("account.regenerateOffer")}
          </Button>
          <Button variant="ghost" onClick={() => setMode("disabling")}>
            {t("account.disableOffer")}
          </Button>
        </div>
      </>
    );
  } else {
    body = (
      <>
        {disabled ? (
          <p
            ref={notice}
            role="status"
            tabIndex={-1}
            className="text-sm focus-visible:outline-none"
          >
            {t("account.disabled")}
          </p>
        ) : null}
        <p className="text-muted">{t("account.mfaOff")}</p>
        <Button
          variant="primary"
          size="lg"
          className="w-full"
          onClick={() => {
            setDisabled(false);
            setMode("enrolling");
          }}
        >
          {t("account.enable")}
        </Button>
      </>
    );
  }

  return (
    <>
      <section
        aria-labelledby="mfa-title"
        aria-busy={status.isPending}
        className="flex flex-col gap-4"
      >
        <h2 id="mfa-title" className="text-lg font-semibold">
          {t("account.mfaTitle")}
        </h2>
        {body}
      </section>
      <p className="text-sm" hidden={busy}>
        <Link href="/o" className={LINK}>
          {t("account.back")}
        </Link>
      </p>
      {busy ? null : <SessionActions />}
    </>
  );
}
