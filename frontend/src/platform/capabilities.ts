import { useEffect, useState } from "react";

/**
 * Whether this browser can create a WebGL2 context, which the 3D world needs. Without it the
 * world is shown as the accessible 2D outline. The probe context is released immediately.
 */
export function supportsWebGL2(doc: Document = document): boolean {
  try {
    const gl = doc.createElement("canvas").getContext("webgl2");
    if (!gl) return false;
    gl.getExtension("WEBGL_lose_context")?.loseContext();
    return true;
  } catch {
    return false;
  }
}

export function useWebGL2(): boolean {
  const [supported] = useState(() => supportsWebGL2());
  return supported;
}

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

export function prefersReducedMotion(win: Window = window): boolean {
  return typeof win.matchMedia === "function" && win.matchMedia(REDUCED_MOTION).matches;
}

/** The user's reduced-motion preference, following changes while the page is open. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => prefersReducedMotion());
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(REDUCED_MOTION);
    const update = () => setReduced(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return reduced;
}
