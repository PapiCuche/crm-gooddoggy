"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Power, PowerOff } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { isStepUpCancelled, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import {
  aiSwitchOff,
  aiSwitchOn,
  aiSwitchRetrieve,
  getAiSwitchRetrieveQueryKey,
} from "@/lib/api/client";
import type { AiSwitch } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { aiError, toLogin } from "./ai-key-actions";

export const MANAGE = "ai.manage";
export const KILL_SWITCH = "ai.kill_switch";
const OUTLINE = "border-border min-h-11 border sm:min-h-9";
type Said = { text: string; alert?: boolean } | null;

// El interruptor de la IA (F8-06, ADR-034 §5 y §10): el de la organización y el del despliegue
// (`AI_ENABLED`, solo lectura: se cambia en el servidor). La IA llama solo con los dos encendidos.
// Apagar es fácil: `ai.kill_switch` o `ai.manage`, una pulsación, sin confirmación ni segundo
// factor, y no espera a nada más que a su propia petición. Encender es difícil: `ai.manage`, la
// confirmación del aviso de datos (D-F8-04) y el segundo factor si la API lo pide.
export function AiSwitchPanel() {
  const t = useTranslations("ai.switch");
  const own = useTranslations("ai.errors");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const held = new Set(permissions.map((grant) => grant.code));
  const manage = held.has(MANAGE);
  const allowed = manage || held.has(KILL_SWITCH);
  const queryClient = useQueryClient();
  const key = getAiSwitchRetrieveQueryKey(slug);
  const query = useQuery<AiSwitch, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => aiSwitchRetrieve(slug, { signal }),
    enabled: allowed,
    retry: false,
  });
  const { withStepUp, dialog } = useStepUp();
  const [busy, setBusy] = useState(false);
  const sending = useRef(false); // dos pulsaciones seguidas no son dos peticiones
  const [said, setSaid] = useState<Said>(null);
  const [asking, setAsking] = useState(false); // el grupo para encender, abierto
  const [noticed, setNoticed] = useState(false);
  const [missing, setMissing] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const notice = useRef<HTMLInputElement>(null);
  const title = useId();
  useEffect(() => {
    if (asking) notice.current?.focus();
  }, [asking]);
  if (!allowed) return null;

  // Lo que se dice de un fallo, por `code`; `null`: nada (cancelar el código, o ir al login).
  const problem = (error: ApiError): string | null => {
    const cancelled: boolean = isStepUpCancelled(error); // cancelar el código no es un fallo
    if (cancelled || toLogin(error)) return null;
    if (error.code === "PERMISSION_DENIED") return own("denied");
    return aiError(error, own)?.text ?? errors(`api.${apiErrorKey(error)}`);
  };
  function change(run: () => Promise<AiSwitch>, done: (value: AiSwitch) => string) {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setSaid(null);
    run()
      .then(
        (value) => {
          queryClient.setQueryData(key, value);
          setAsking(false);
          setSaid({ text: done(value) });
          setTimeout(() => heading.current?.focus()); // el botón pulsado ya no está
        },
        (error: ApiError) => {
          const text = problem(error);
          setSaid(text ? { text, alert: true } : null);
        },
      )
      .finally(() => {
        sending.current = false;
        setBusy(false);
      });
  }
  const turnOff = () =>
    change(
      () => aiSwitchOff(slug),
      () => t("turnOff.done"),
    );
  const turnOn = () => {
    if (!noticed) {
      setMissing(true);
      notice.current?.focus();
      return;
    }
    change(
      () => withStepUp(() => aiSwitchOn(slug, { notice_confirmed: true })),
      (value) => t(value.global_enabled ? "turnOn.done" : "turnOn.doneGlobalOff"),
    );
  };
  const open = () => {
    setSaid(null);
    setNoticed(false);
    setMissing(false);
    setAsking(true);
  };

  const data = query.data;
  const alert = said?.alert ? (
    <p role="alert" className="text-danger text-sm">
      {said.text}
    </p>
  ) : null;
  let body;
  if (data) {
    const on = data.enabled && data.global_enabled;
    const Icon = on ? Power : PowerOff;
    const word = (value: boolean) => t(value ? "state.ON" : "state.OFF");
    body = (
      <>
        <p className="flex items-center gap-2 font-medium">
          <Icon aria-hidden className={cn("size-4 shrink-0", on ? "text-success" : "text-muted")} />
          {t(on ? "effective.ON" : "effective.OFF")}
        </p>
        <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
          <dt className="text-muted">{t("organization")}</dt>
          <dd>{word(data.enabled)}</dd>
          <dt className="text-muted">{t("system")}</dt>
          <dd>{word(data.global_enabled)}</dd>
          <dt className="text-muted">{t("changed")}</dt>
          <dd>
            {data.changed_at ? (
              <time dateTime={data.changed_at}>
                {format.dateTime(new Date(data.changed_at), {
                  ...{ day: "numeric", month: "short", year: "numeric" },
                  ...{ hour: "2-digit", minute: "2-digit" },
                })}
              </time>
            ) : (
              t("never")
            )}
          </dd>
        </dl>
        {data.global_enabled ? null : <p className="text-sm">{t("globalOff")}</p>}
        {data.enabled ? (
          <Button
            variant="primary"
            className="min-h-11 self-start sm:min-h-9"
            aria-disabled={busy}
            onClick={turnOff}
          >
            {busy ? t("turnOff.busy") : t("turnOff.open")}
          </Button>
        ) : !manage ? (
          <p className="text-muted text-sm">{t("onlyManage")}</p>
        ) : asking ? (
          <div
            role="group"
            aria-label={t("turnOn.title")}
            className="border-border flex w-full max-w-xl flex-col gap-3 rounded-lg border p-3"
          >
            <label className="flex min-h-11 items-start gap-2 text-sm">
              <input
                ref={notice}
                type="checkbox"
                className="mt-0.5"
                checked={noticed}
                aria-invalid={missing && !noticed}
                aria-describedby={missing && !noticed ? `${title}-missing` : undefined}
                onChange={(event) => setNoticed(event.target.checked)}
              />
              {t("turnOn.notice")}
            </label>
            {missing && !noticed ? (
              <p id={`${title}-missing`} role="alert" className="text-danger text-sm">
                {t("turnOn.required")}
              </p>
            ) : null}
            {alert}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="ghost"
                className={OUTLINE}
                aria-disabled={busy}
                onClick={() => !sending.current && setAsking(false)}
              >
                {t("turnOn.cancel")}
              </Button>
              <Button
                variant="primary"
                className="min-h-11 sm:min-h-9"
                aria-disabled={busy}
                onClick={turnOn}
              >
                {busy ? t("turnOn.busy") : t("turnOn.submit")}
              </Button>
            </div>
          </div>
        ) : (
          <Button variant="ghost" className={cn(OUTLINE, "self-start")} onClick={open}>
            {t("turnOn.open")}
          </Button>
        )}
        {asking && !data.enabled ? null : alert}
      </>
    );
  } else if (query.isError && query.error.status !== 401) {
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className="text-danger">
          {query.error.code === "PERMISSION_DENIED"
            ? t("denied")
            : errors(`api.${apiErrorKey(query.error)}`)}
        </p>
        <Button variant="primary" size="lg" onClick={() => void query.refetch()}>
          {errors("retry")}
        </Button>
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <section
      aria-labelledby={title}
      className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4"
    >
      <h2 id={title} ref={heading} tabIndex={-1} className="font-medium focus-visible:outline-none">
        {t("title")}
      </h2>
      <p className="text-muted text-sm">{t("intro", { organization: organization.name })}</p>
      {body}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={said && !said.alert ? "text-sm" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {dialog}
    </section>
  );
}
