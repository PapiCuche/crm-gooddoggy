import { act, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/components/app-shell/tenant-context";
import type { SelfContext } from "@/lib/api/model";

import { Drafts, useDrafts } from "./drafts";

// El almacén de borradores (UX-INBOX-04). El recorrido en el Inbox está en `composer.test.tsx`.
describe("el almacén de borradores", () => {
  it("separa conversación y modo; solo espacios no es borrador; otra organización no escribe", () => {
    const acme = new Drafts("o1");
    const [reply, note] = [acme.key("1", "reply"), acme.key("1", "note")];
    acme.update(reply, () => ({ text: "Hola" }));
    acme.update(note, () => ({ text: "  " }));
    expect([acme.get(reply).text, acme.get(note).text, acme.get(acme.key("2", "reply"))]).toEqual([
      "Hola",
      "  ",
      { text: "", attempt: null },
    ]);
    expect([acme.has("1"), acme.has("2")]).toEqual([true, false]);
    acme.update(reply, () => ({ text: "" }));
    expect(acme.has("1")).toBe(false);
    const beta = new Drafts("o2"); // los mismos ids de conversación
    beta.update(reply, () => ({ text: "de Acme" }));
    expect([beta.get(reply).text, beta.key("1", "reply") === reply]).toEqual(["", false]);
  });

  it("al desmontarse (cerrar sesión, otra organización) se vacía, avisa y no acepta nada más", () => {
    let drafts!: Drafts;
    function Probe() {
      drafts = useDrafts();
      return null;
    }
    const tenant = { user: { id: "u1" }, organization: { id: "o1" } } as SelfContext;
    const view = render(
      <TenantProvider value={tenant}>
        <Probe />
      </TenantProvider>,
    );
    const key = drafts.key("1", "reply");
    const heard = vi.fn();
    drafts.subscribe(heard);
    const attempt = { id: "a", first: "Hola", media: false };
    act(() => drafts.update(key, () => ({ text: "Hola", attempt })));
    view.unmount();
    expect(drafts.get(key)).toEqual({ text: "", attempt: null });
    drafts.update(key, () => ({ text: "tarde" })); // un envío que termina después
    expect([drafts.get(key).text, heard.mock.calls.length]).toEqual(["", 2]);
  });
});
