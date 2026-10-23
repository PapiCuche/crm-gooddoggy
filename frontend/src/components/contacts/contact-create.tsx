"use client";

import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { Choice } from "@/components/catalog/product-create";
import { FieldsForm } from "@/components/forms/fields-form";
import { Button } from "@/components/ui/button";
import { useContactsCreate } from "@/lib/api/client";
import {
  type ContactCreateRequest,
  ContactDocumentTypeEnum,
  type ContactSourceEnum,
} from "@/lib/api/model";

// Los límites son los del contrato. El número va el último: su tipo es el `select` que sigue.
const FIELDS = [
  { name: "first_name", max: 100 },
  { name: "last_name", max: 100 },
  { name: "display_name", max: 200, hinted: true },
  { name: "primary_phone_e164", max: 32, inputMode: "tel", literal: true, hinted: true },
  { name: "primary_email", max: 254, inputMode: "email", literal: true },
  { name: "document_number", max: 32, literal: true, hinted: true },
] as const;
type Name = (typeof FIELDS)[number]["name"];
// Los orígenes que una persona elige al registrar un cliente. `IMPORT` y `AI` los pone el
// sistema (una importación, el asistente), no este formulario.
const SOURCES: ContactSourceEnum[] = [
  "MANUAL",
  "WALK_IN",
  "REFERRAL",
  "INBOUND_MESSAGE",
  "CAMPAIGN",
];

// Crear un cliente (F4-04) con `POST /api/v1/o/{slug}/contacts/` (F4-03). La API pone a quien
// lo crea como responsable, y la sucursal de su membresía. Solo viaja lo escrito: sin nombre
// para mostrar, la API lo forma con el nombre y el apellido, y si no hay ninguno de los tres lo
// dice en ese campo. El teléfono no se completa: la ayuda propone el +51, la API exige E.164.
export function ContactCreate({
  slug,
  listKey,
  onAsk,
  onDone,
}: {
  slug: string;
  listKey: QueryKey;
  onAsk: () => void; // se abre el formulario: el anuncio anterior ya no aplica
  onDone: (text: string) => void;
}) {
  const t = useTranslations("contacts.create");
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [documentType, setDocumentType] = useState<ContactDocumentTypeEnum | "">("");
  const [source, setSource] = useState<ContactSourceEnum>("MANUAL");
  const trigger = useRef<HTMLButtonElement>(null);
  // `networkMode`: sin red falla y se dice; una escritura no queda en cola para después.
  const create = useContactsCreate({
    mutation: {
      networkMode: "always",
      onSuccess: async (contact) => {
        // Sigue «Creando…» hasta que la lista vuelve. Si releerla falla, se dice.
        await queryClient.invalidateQueries({ queryKey: listKey });
        const stale = queryClient.getQueryState(listKey)?.status === "error";
        setOpen(false);
        onDone(t(stale ? "doneStale" : "done", { name: contact.display_name }));
      },
    },
  });
  const busy = create.isPending;

  const opened = useRef(false);
  useEffect(() => {
    // Al cerrar, el foco vuelve a «Crear cliente», salvo que el usuario ya esté en otra parte.
    if (!open && opened.current && document.activeElement === document.body) {
      trigger.current?.focus();
    }
    opened.current = open;
  }, [open]);

  function show(next: boolean) {
    create.reset();
    if (next) {
      setDocumentType("");
      setSource("MANUAL");
      onAsk();
    }
    setOpen(next);
  }

  return (
    <div
      className="flex flex-col items-start gap-3"
      // Enter mantenido repite la pulsación: reabriría el formulario o reenviaría sin parar.
      onKeyDown={(event) => event.repeat && event.key === "Enter" && event.preventDefault()}
    >
      {open ? (
        <FieldsForm<Name>
          title={t("title")}
          heading={<h2 className="font-medium">{t("title")}</h2>}
          className="border-border bg-surface flex w-full max-w-xl flex-col gap-4 rounded-lg border p-4"
          fields={FIELDS.map((field) => ({
            ...field,
            label: t(field.name),
            // Un número sin su tipo no se envía: la API lo rechazaría por ir sin él.
            invalid:
              field.name === "document_number" && !documentType
                ? t("documentTypeMissing")
                : t(`${field.name}Invalid`),
            valid: field.name === "document_number" ? () => !!documentType : undefined,
            hint: "hinted" in field ? t(`${field.name}Hint`) : undefined,
          }))}
          note={
            <>
              <Choice
                label={t("document_type")}
                value={documentType}
                busy={busy}
                onChange={(value) => setDocumentType(value as ContactDocumentTypeEnum | "")}
                options={[
                  ["", t("noDocument")],
                  ...Object.values(ContactDocumentTypeEnum).map((code): [string, string] => [
                    code,
                    t(`documentType.${code}`),
                  ]),
                ]}
              />
              <Choice
                label={t("source")}
                value={source}
                hint={t("sourceHint")}
                busy={busy}
                onChange={(value) => setSource(value as ContactSourceEnum)}
                options={SOURCES.map((code) => [code, t(`sources.${code}`)])}
              />
            </>
          }
          write={create}
          // Solo lo escrito, sin espacios exteriores; el origen siempre, porque se ve elegido.
          send={(values) => {
            const data: ContactCreateRequest = { source };
            for (const { name } of FIELDS) if (values[name]) data[name] = values[name];
            if (documentType) data.document_type = documentType;
            create.mutate({ orgSlug: slug, data });
          }}
          // El tipo sin número: la API lo dice en el tipo; aquí, junto al número que falta.
          taken={(error) =>
            error.code === "VALIDATION_ERROR" &&
            error.fields &&
            Object.hasOwn(error.fields, "document_type")
              ? { field: "document_number", text: t("documentNumberMissing") }
              : null
          }
          denied={t("denied")}
          labels={{ submit: t("submit"), busy: t("busy"), cancel: t("cancel") }}
          onCancel={() => show(false)}
        />
      ) : (
        <Button
          ref={trigger}
          variant="accent"
          className="min-h-11 sm:min-h-9"
          onClick={() => show(true)}
        >
          {t("open")}
        </Button>
      )}
    </div>
  );
}
