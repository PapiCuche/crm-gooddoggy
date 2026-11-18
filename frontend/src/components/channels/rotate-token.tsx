"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";

import { channelError } from "./channel-errors";
import { type Route, rotateToken } from "./channel-routes";
import { useChannelWrite } from "./use-channel-write";

// Rotar el token de una cuenta (F7-07b; Messenger e Instagram en F7b-06):
// `POST …/channels/{whatsapp|messenger|instagram}/{id}/token/`. Mismo trato del secreto que al
// conectar: campo de contraseña que se vacía al enviar y que nunca se guarda.
export function RotateToken({
  slug,
  route,
  accountId,
  onRotated,
}: {
  slug: string;
  route: Route;
  accountId: string;
  onRotated: () => void;
}) {
  const t = useTranslations("channels.rotate");
  const errors = useTranslations("channels.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const social = route !== "whatsapp";
  // Instagram y Messenger de una misma página tienen cada uno su token (ADR-035 §3): se dice.
  const hint = social ? "socialHint" : "hint";

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h3 className="font-medium">{t("title")}</h3>}
          className="border-border flex w-full max-w-xl flex-col gap-3 rounded-lg border p-3"
          fields={[
            {
              ...{ name: "access_token", max: 2048, label: t("token"), hint: t(hint) },
              ...{ invalid: t("invalid"), required: t("required"), secret: true, literal: true },
            },
          ]}
          write={write}
          send={(values) =>
            run(
              () => rotateToken(slug, route, accountId, values.access_token),
              () => {
                setDone(true);
                setOpen(false);
                onRotated();
                setTimeout(() => trigger.current?.focus());
              },
              // La cuenta ya no está (404): se vuelve a leer la lista, como al desconectar.
              (error) => error.status === 404 && onRotated(),
            )
          }
          taken={(error) => channelError(error, errors, social)}
          denied={errors("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => {
            setOpen(false);
            setTimeout(() => trigger.current?.focus());
          }}
        />
      ) : (
        <Button
          ref={trigger}
          variant="ghost"
          className="border-border min-h-11 border sm:min-h-9"
          onClick={() => {
            write.reset();
            setDone(false);
            setOpen(true);
          }}
        >
          {t("open")}
        </Button>
      )}
      <p role="status" className={done ? "text-sm" : "sr-only"}>
        {done ? t("done") : ""}
      </p>
      {stepUp}
    </div>
  );
}
