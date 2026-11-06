"use client";

import { useTranslations } from "next-intl";
import { useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { channelsWhatsappRotateToken } from "@/lib/api/client";

import { channelError } from "./channel-errors";
import { useChannelWrite } from "./use-channel-write";

// Rotar el token de una cuenta (F7-07b): `POST …/channels/whatsapp/{id}/token/`. Mismo trato del
// secreto que al conectar: campo de contraseña que se vacía al enviar y que nunca se guarda.
export function RotateToken({
  slug,
  accountId,
  onRotated,
}: {
  slug: string;
  accountId: string;
  onRotated: () => void;
}) {
  const t = useTranslations("channels.rotate");
  const errors = useTranslations("channels.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  return (
    <div className="flex min-w-0 flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h3 className="font-medium">{t("title")}</h3>}
          className="border-border flex w-full max-w-xl flex-col gap-3 rounded-lg border p-3"
          fields={[
            {
              ...{ name: "access_token", max: 2048, label: t("token"), hint: t("hint") },
              ...{ invalid: t("invalid"), required: t("required"), secret: true, literal: true },
            },
          ]}
          write={write}
          send={(values) =>
            run(
              () => channelsWhatsappRotateToken(slug, accountId, values),
              () => {
                setDone(true);
                setOpen(false);
                onRotated();
                setTimeout(() => trigger.current?.focus());
              },
            )
          }
          taken={(error) => channelError(error, errors)}
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
