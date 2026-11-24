import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  useSearchParams: (await import("./src/test-search-params")).useSearchParams,
}));

// Sin `globals`, Testing Library no limpia el DOM entre tests; la URL tampoco pasa de uno a otro.
afterEach(() => (cleanup(), globalThis.history?.replaceState(null, "", "/")));
