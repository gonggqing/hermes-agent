import { describe, expect, it } from "vitest";

import { randomId } from "./utils";

describe("randomId", () => {
  it("returns a unique id when crypto.randomUUID exists", () => {
    expect(randomId()).not.toBe(randomId());
    expect(randomId().length).toBeGreaterThan(8);
  });

  it("does not throw on a NON-secure origin (crypto.randomUUID undefined)", () => {
    // http://my.hermes:9119 has no randomUUID — the bug that broke draft actions.
    const orig = globalThis.crypto.randomUUID;
    // @ts-expect-error simulate a non-secure context
    globalThis.crypto.randomUUID = undefined;
    try {
      const id = randomId();
      expect(typeof id).toBe("string");
      expect(id.length).toBeGreaterThan(8);
      expect(randomId()).not.toBe(id);
    } finally {
      globalThis.crypto.randomUUID = orig;
    }
  });
});
