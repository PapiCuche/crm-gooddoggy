"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { Button } from "@/components/ui/button";
import { automationsRuleUpdate, getAutomationsRetrieveQueryKey } from "@/lib/api/client";
import type { AutomationModeEnum, AutomationRule } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { text } from "@/lib/message-text";

import messages from "../../../messages/es-PE.json";

const MODES: readonly AutomationModeEnum[] = ["OFF", "SIMULATE", "ON"];
const WEEK = [1, 2, 3, 4, 5, 6, 7];
// Los rangos de `apps.automations.params`: el navegador avisa antes; la API valida de nuevo.
const NUMBERS: Record<string, readonly [number, number]> = {
  ...{ first_after_minutes: [1, 1440], escalate_after_minutes: [1, 1440], max_recipients: [1, 3] },
  ...{ max_open_per_user: [1, 100], after_hours: [1, 720], before_expiry_hours: [0, 720] },
};
const OWN = ["SIMULATION_TOO_SHORT", "STALE_VERSION", "VALIDATION_ERROR"];
const FIELD = "border-foreground/50 bg-surface h-11 w-full rounded-[10px] border px-3 text-[16px]";
export const WHEN = { dateStyle: "medium", timeStyle: "short" } as const;
// Un día ISO (1 = lunes) con el nombre corto del idioma: el 1 de enero de 2024 fue lunes.
const dayOf = (day: number) => new Date(Date.UTC(2024, 0, day));
export type Said = { text: string; alert?: boolean } | null;

// Una regla (F11-08a, ADR-049 §2, E2): su modo y sus parámetros. Con `automations.manage` se edita; sin él, se lee. `ON` solo
// se ofrece tras 7 días en SIMULATE (`on_allowed_at`, reloj de la base) y con la revisión del
// propietario marcada; es comodidad: la API rechaza `ON` antes de tiempo y aquí se enseña.
export function AutomationRuleCard({ rule, manage }: { rule: AutomationRule; manage: boolean }) {
  const t = useTranslations("automations");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization } = useTenant();
  const queryClient = useQueryClient();
  const key = getAutomationsRetrieveQueryKey(organization.slug);
  const name = text(messages.automations.rules, `${rule.key}.name`) ?? rule.key;
  const id = useId();
  // Lo que se edita; vuelve a lo guardado cuando la regla cambia de versión (al guardar o releer).
  const [draft, setDraft] = useState({ mode: rule.mode, params: rule.params, reviewed: false });
  const [base, setBase] = useState(rule.version);
  if (base !== rule.version) {
    setBase(rule.version);
    setDraft({ mode: rule.mode, params: rule.params, reviewed: false });
  }
  const [said, setSaid] = useState<Said>(null);
  const [missing, setMissing] = useState(false);
  const sending = useRef(false);
  const [busy, setBusy] = useState(false);
  const review = useRef<HTMLInputElement>(null);
  const [now] = useState(() => Date.now()); // la hora al abrir la pantalla
  const when = (at: string) => format.dateTime(new Date(at), WHEN);
  const allowedAt = rule.mode === "SIMULATE" ? rule.on_allowed_at : null;
  const onAllowed = rule.mode === "ON" || (!!allowedAt && Date.parse(allowedAt) <= now);
  const toOn = draft.mode === "ON" && rule.mode !== "ON";

  async function save(event: FormEvent) {
    event.preventDefault();
    if (sending.current) return;
    const params = Object.fromEntries(
      Object.entries(draft.params).filter(
        ([k, v]) => JSON.stringify(v) !== JSON.stringify(rule.params[k]),
      ),
    );
    const mode = draft.mode === rule.mode ? undefined : draft.mode;
    if (!mode && Object.keys(params).length === 0) return setSaid({ text: t("unchanged") });
    if (toOn && !draft.reviewed) {
      setMissing(true);
      return review.current?.focus();
    }
    sending.current = true;
    setBusy(true);
    setSaid(null);
    try {
      const body = { version: rule.version, ...(mode ? { mode } : {}) };
      const value = await automationsRuleUpdate(organization.slug, rule.key, {
        ...body,
        ...(Object.keys(params).length ? { params } : {}),
      });
      await queryClient.cancelQueries({ queryKey: key });
      queryClient.setQueryData(key, value);
      setSaid({ text: t("saved", { name }) });
    } catch (error) {
      const code = (error as ApiError).code;
      if (code === "STALE_VERSION") void queryClient.invalidateQueries({ queryKey: key });
      const own = OWN.includes(code);
      setSaid({
        alert: true,
        text: own ? t(`errors.${code}`) : errors(`api.${apiErrorKey(error)}`),
      });
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  const set = (param: string, value: unknown) =>
    setDraft((before) => ({ ...before, params: { ...before.params, [param]: value } }));
  const days = (draft.params.days as number[] | undefined) ?? [];
  const label = (param: string) => t(`params.${param}`);
  const weekday = (day: number) =>
    format.dateTime(dayOf(day), { weekday: "short", timeZone: "UTC" });
  const shown = (param: string, value: unknown) =>
    param === "days" && Array.isArray(value) ? value.map(weekday).join(", ") : String(value);

  const explain =
    rule.mode === "ON" ? null : (
      <div id={`${id}-on`} className="text-muted flex flex-col gap-1 text-sm">
        <p>{t("on.rule")}</p>
        <p>
          {rule.mode === "OFF"
            ? t("on.needsSimulate")
            : onAllowed
              ? t("on.ready")
              : allowedAt
                ? t("on.wait", {
                    left: format.relativeTime(new Date(allowedAt), now),
                    at: when(allowedAt),
                  })
                : null}
        </p>
      </div>
    );

  return (
    <section
      aria-labelledby={`${id}-title`}
      className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={`${id}-title`} className="font-medium">
          {name}
        </h2>
        <span className="bg-surface-raised rounded-md px-2 py-0.5 font-mono text-sm">
          {rule.mode}
        </span>
      </div>
      <p className="text-muted text-sm">{text(messages.automations.rules, `${rule.key}.about`)}</p>
      {manage ? (
        <form onSubmit={(event) => void save(event)} className="flex flex-col gap-4" noValidate>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">{t("mode.label")}</legend>
            {MODES.map((mode) => (
              <label key={mode} className="flex min-h-11 items-center gap-2 text-sm sm:min-h-8">
                <input
                  type="radio"
                  name={`${id}-mode`}
                  value={mode}
                  className="size-4 shrink-0"
                  checked={draft.mode === mode}
                  disabled={mode === "ON" && !onAllowed}
                  aria-describedby={mode === "ON" && explain ? `${id}-on` : undefined}
                  onChange={() =>
                    mode === "ON" && !onAllowed ? null : setDraft((b) => ({ ...b, mode }))
                  }
                />
                {t(`mode.${mode}`)}
              </label>
            ))}
          </fieldset>
          {explain}
          {toOn ? (
            <label className="flex min-h-11 items-start gap-2 text-sm">
              <input
                ref={review}
                type="checkbox"
                className="mt-0.5 size-4 shrink-0"
                checked={draft.reviewed}
                aria-invalid={missing && !draft.reviewed}
                onChange={(event) => setDraft((b) => ({ ...b, reviewed: event.target.checked }))}
              />
              {t("on.reviewed")}
            </label>
          ) : null}
          {toOn && missing && !draft.reviewed ? (
            <p role="alert" className="text-danger text-sm">
              {t("on.required")}
            </p>
          ) : null}
          <fieldset className="grid gap-3 sm:grid-cols-2">
            <legend className="mb-2 text-sm font-medium">{t("params.title")}</legend>
            {Object.entries(draft.params).map(([param, value]) =>
              param === "days" ? (
                <fieldset key={param} className="flex flex-col gap-1.5 sm:col-span-2">
                  <legend className="mb-1 text-sm">{label(param)}</legend>
                  <div className="flex flex-wrap gap-x-4">
                    {WEEK.map((day) => (
                      <label
                        key={day}
                        className="flex min-h-11 items-center gap-2 text-sm sm:min-h-8"
                      >
                        <input
                          type="checkbox"
                          className="size-4"
                          checked={days.includes(day)}
                          onChange={({ target }) =>
                            set(
                              "days",
                              WEEK.filter((d) => (d === day ? target.checked : days.includes(d))),
                            )
                          }
                        />
                        {weekday(day)}
                      </label>
                    ))}
                  </div>
                </fieldset>
              ) : (
                <label key={param} className="flex min-w-0 flex-col gap-1.5 text-sm">
                  {label(param)}
                  <input
                    className={FIELD}
                    type={NUMBERS[param] ? "number" : "time"}
                    min={NUMBERS[param]?.[0]}
                    max={NUMBERS[param]?.[1]}
                    value={String(value)}
                    onChange={({ target }) =>
                      set(
                        param,
                        NUMBERS[param] && target.value !== "" ? Number(target.value) : target.value,
                      )
                    }
                  />
                </label>
              ),
            )}
          </fieldset>
          <Button variant="primary" className="min-h-11 self-start sm:min-h-9" aria-disabled={busy}>
            {busy ? t("saving") : t("save")}
          </Button>
        </form>
      ) : (
        <>
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
            <dt className="text-muted">{t("mode.label")}</dt>
            <dd>{t(`mode.${rule.mode}`)}</dd>
            {Object.entries(rule.params).map(([param, value]) => (
              <div key={param} className="contents">
                <dt className="text-muted">{label(param)}</dt>
                <dd>{shown(param, value)}</dd>
              </div>
            ))}
          </dl>
          {explain}
          <p className="text-muted text-sm">{t("readOnly")}</p>
        </>
      )}
      <Result said={said} />
    </section>
  );
}

// Lo que pasó al guardar, pausar o reanudar. El estado siempre está montado: un lector de
// pantalla anuncia el resultado cuando cambia; un fallo es una alerta.
export function Result({ said }: { said: Said }) {
  const ok = said && !said.alert;
  return (
    <>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm">
          {said.text}
        </p>
      ) : null}
      <p role="status" className={ok ? "text-sm" : "sr-only"}>
        {ok ? said.text : ""}
      </p>
    </>
  );
}
