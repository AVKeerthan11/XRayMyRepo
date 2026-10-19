import { Link, Outlet } from "react-router";

/** The frame around every page: a skip link, the wordmark and the main landmark. */
export function Shell() {
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#content"
        className="sr-only z-50 rounded-md bg-signal px-4 py-2 font-medium text-void focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <header className="flex items-center justify-between px-5 pt-5 sm:px-10 sm:pt-8">
        <Link to="/" className="group flex items-baseline gap-3">
          <span className="text-lg font-semibold tracking-tight text-ink">XRayMyRepo</span>
          <span className="text-xs tracking-[0.3em] text-faint uppercase group-hover:text-signal">Observatory</span>
        </Link>
      </header>
      <main id="content" tabIndex={-1} className="flex-1 px-5 pt-8 pb-16 outline-none sm:px-10 sm:pt-12">
        <Outlet />
      </main>
    </div>
  );
}
