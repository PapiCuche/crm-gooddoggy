import { expect, it, vi } from "vitest";

import { allPages } from "./all-pages";

it("sigue el cursor hasta la última página y devuelve las filas en orden", async () => {
  const pages: Record<string, { results: number[]; next?: string | null }> = {
    "": { results: [1, 2], next: "b" },
    b: { results: [], next: "c" }, // una página vacía no es la última
    c: { results: [3] }, // sin `next`: la última
  };
  const fetchPage = vi.fn(async (cursor: string | undefined) => pages[cursor ?? ""]!);
  expect(await allPages(fetchPage)).toEqual([1, 2, 3]);
  expect(fetchPage.mock.calls.map(([cursor]) => cursor)).toEqual([undefined, "b", "c"]);
});

it("un cursor que no avanza es un error, no una lista sin fin", async () => {
  const fetchPage = vi.fn(async (cursor: string | undefined) => ({
    results: [cursor ?? "primera"],
    next: cursor === "a" ? "b" : "a", // a → b → a…
  }));
  await expect(allPages(fetchPage)).rejects.toMatchObject({ status: 500, code: "INTERNAL_ERROR" });
  expect(fetchPage).toHaveBeenCalledTimes(3);
});
