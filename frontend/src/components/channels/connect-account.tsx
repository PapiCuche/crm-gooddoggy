"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { channelsWhatsappConnect } from "@/lib/api/client";

import { channelError } from "./channel-errors";
import { useChannelWrite } from "./use-channel-write";

// Conectar una cuenta de WhatsApp (F7-07b): `POST …/channels/whatsapp/` con el WABA, el
// `phone_number_id`, el token y un nombre. El token es un campo de contraseña que se vacía al
// enviar (`secret`) y solo vive en el cierre de la petición: no se guarda en estado, en la caché
// ni en el navegador. Un número no registrable da el mismo error sin más detalle.
export function ConnectAccount({ slug, onConnected }: { slug: string; onConnected: () => void }) {
  const t = useTranslations("channels.connect");
  const errors = useTranslations("channels.errors");
  const { write, run, stepUp } = useChannelWrite();
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar desaparece el control pulsado: el foco vuelve al botón, salvo que ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  return (
    <div className="flex flex-col items-start gap-3">
      {open ? (
        <FieldsForm
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={[
            {
              ...{ name: "name", max: 100, label: t("name"), invalid: t("nameInvalid") },
              required: t("nameRequired"),
            },
            {
              ...{ name: "waba_id", max: 30, label: t("waba"), invalid: t("wabaInvalid") },
              ...{ required: t("wabaRequired"), inputMode: "numeric", literal: true },
            },
            {
              ...{ name: "phone_number_id", max: 30, label: t("phone") },
              // El mismo texto para cualquier número que no se pueda registrar: no se descompone.
              ...{ invalid: errors("badNumber"), required: t("phoneRequired") },
              ...{ inputMode: "numeric", literal: true },
            },
            {
              ...{ name: "access_token", max: 2048, label: t("token"), hint: t("tokenHint") },
              ...{ invalid: t("tokenInvalid"), required: t("tokenRequired"), secret: true },
              literal: true,
            },
          ]}
          write={write}
          send={(values) =>
            run(
              () => channelsWhatsappConnect(slug, values),
              () => {
                setDone(values.name);
                setOpen(false);
                onConnected();
              },
            )
          }
          taken={(error) => channelError(error, errors)}
          denied={errors("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          note={<p className="text-muted text-sm">{t("note")}</p>}
          onCancel={() => setOpen(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => {
            write.reset();
            setDone(null);
            setOpen(true);
          }}
        >
          {t("open")}
        </Button>
      )}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={done ? "text-sm wrap-anywhere" : "sr-only"}>
        {done ? t("done", { name: done }) : ""}
      </p>
      {stepUp}
    </div>
  );
}
