import { afterEach, describe, expect, it, vi } from "vitest";

import { prefersReducedMotion, supportsWebGL2 } from "./capabilities";

afterEach(() => {
  vi.unstubAllGlobals();
});

function canvasReturning(context: unknown): Document {
  const canvas = { getContext: vi.fn(() => context) };
  return { createElement: () => canvas } as unknown as Document;
}

describe("supportsWebGL2", () => {
  it("is true when a WebGL2 context can be created, and releases it", () => {
    const loseContext = vi.fn();
    const gl = { getExtension: () => ({ loseContext }) };
    expect(supportsWebGL2(canvasReturning(gl))).toBe(true);
    expect(loseContext).toHaveBeenCalled();
  });

  it("is false without a context, or when creating one throws", () => {
    expect(supportsWebGL2(canvasReturning(null))).toBe(false);
    const throwing = {
      createElement: () => ({
        getContext: () => {
          throw new Error("GPU process crashed");
        },
      }),
    } as unknown as Document;
    expect(supportsWebGL2(throwing)).toBe(false);
  });

  it("is false in jsdom, which has no WebGL", () => {
    expect(supportsWebGL2()).toBe(false);
  });
});

describe("prefersReducedMotion", () => {
  it("follows the media query", () => {
    const win = (matches: boolean) => ({ matchMedia: () => ({ matches }) }) as unknown as Window;
    expect(prefersReducedMotion(win(true))).toBe(true);
    expect(prefersReducedMotion(win(false))).toBe(false);
  });

  it("is false where matchMedia does not exist", () => {
    expect(prefersReducedMotion({} as Window)).toBe(false);
  });
});
