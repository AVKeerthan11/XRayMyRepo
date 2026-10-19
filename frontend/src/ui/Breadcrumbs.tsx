import { Link } from "react-router";

export interface Crumb {
  label: string;
  to?: string;
  mono?: boolean;
}

/** Where you are in the observatory; the last crumb is the current page. */
export function Breadcrumbs({ crumbs }: { crumbs: readonly Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb">
      <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-mist">
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          const className = crumb.mono ? "font-mono" : undefined;
          return (
            <li key={`${index}-${crumb.label}`} className="flex items-center gap-2">
              {index > 0 && (
                <span aria-hidden="true" className="text-faint">
                  /
                </span>
              )}
              {crumb.to && !last ? (
                <Link to={crumb.to} className={`${className ?? ""} underline-offset-4 hover:text-signal hover:underline`}>
                  {crumb.label}
                </Link>
              ) : (
                <span aria-current={last ? "page" : undefined} className={`${className ?? ""} ${last ? "text-ink" : ""}`}>
                  {crumb.label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
