"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";

import { CHANNEL_MAX, channelFits, type Extra } from "./promotion-rules";

// Lo opcional de «Nueva promoción»: prioridad, canales y «Visible a la IA». Cada error (propio o
// del servidor) cuelga de su campo, que es lo que recibe el foco.
export function PromotionExtras(props: {
  value: Extra;
  problems: Record<string, true>;
  onChange: (change: Partial<Extra>) => void;
}) {
  const { value, problems, onChange } = props;
  const t = useTranslations("promotions.form");
  const [refused, setRefused] = useState(false);
  const small = "min-h-11 sm:min-h-9";

  function add() {
    const name = value.draft.trim();
    if (!channelFits(value.channels, name)) return setRefused(true);
    setRefused(false);
    onChange({ channels: [...value.channels, name], draft: "" });
  }

  return (
    <>
      <TextField
        label={t("priority")}
        value={value.priority}
        inputMode="numeric"
        autoComplete="off"
        maxLength={6}
        hint={t("priorityHint")}
        error={problems.priority ? t("err.priority") : undefined}
        onChange={(event) => onChange({ priority: event.target.value.trim() })}
      />
      <fieldset className="flex flex-col gap-2">
        <legend className="text-sm font-medium">{t("channels")}</legend>
        {value.channels.length ? (
          <ul className="flex flex-wrap gap-2">
            {value.channels.map((name) => (
              <li
                key={name}
                className="border-border flex items-center gap-1 rounded-lg border pl-3"
              >
                <span className="text-sm wrap-anywhere">{name}</span>
                <Button
                  type="button"
                  variant="ghost"
                  className={small}
                  aria-label={t("removeChannel", { name })}
                  onClick={() => onChange({ channels: value.channels.filter((c) => c !== name) })}
                >
                  ×
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm">{t("allChannels")}</p>
        )}
        <TextField
          label={t("channelDraft")}
          value={value.draft}
          maxLength={CHANNEL_MAX * 2}
          autoComplete="off"
          hint={t("channelsHint")}
          error={refused || problems.channels ? t("err.channels") : undefined}
          onChange={(event) => {
            setRefused(false);
            onChange({ draft: event.target.value });
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.nativeEvent.isComposing || event.keyCode === 229)
              return; // IME
            event.preventDefault(); // Enter agrega el canal, no envía el formulario
            if (!event.repeat) add();
          }}
        />
        <Button
          type="button"
          variant="ghost"
          className={`${small} border-border self-start border`}
          onClick={add}
        >
          {t("addChannel")}
        </Button>
      </fieldset>
      <div className="flex flex-col gap-1">
        <label className={`flex items-center gap-2 text-sm ${small}`}>
          <input
            type="checkbox"
            className="size-4 shrink-0"
            checked={value.ai}
            aria-invalid={problems.ai ? true : undefined}
            onChange={(event) => onChange({ ai: event.target.checked })}
          />
          {t("ai")}
        </label>
        <p
          role={problems.ai ? "alert" : undefined}
          className={problems.ai ? "text-danger text-sm" : "text-muted text-sm"}
        >
          {problems.ai ? t("err.ai") : t("aiHint")}
        </p>
      </div>
    </>
  );
}
