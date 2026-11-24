import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Sin el router de Next.js, `useSearchParams` lee la URL de jsdom.
vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  useSearchParams: (await import("./src/test-search-params")).useSearchParams,
}));

afterEach(() => {
  cleanup(); // sin `globals`, Testing Library no limpia el DOM entre tests
  globalThis.history?.replaceState(null, "", "/"); // la URL no pasa de un test a otro (jsdom)
});
