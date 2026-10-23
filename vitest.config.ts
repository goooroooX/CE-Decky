import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    clearMocks: true,
    // A failed query prints the page's rows and their text rather than markup
    // cut off inside the first card; see the file for why.
    setupFiles: ["tests/setup/compactDomErrors.ts"],
  },
});
