import { Link } from "react-router";

export function NotFound() {
  return (
    <div className="mx-auto max-w-3xl">
      <h1 className="text-4xl font-light tracking-tight text-ink">This place is not on the map.</h1>
      <p className="mt-4 text-mist">
        <Link to="/" className="text-signal underline underline-offset-4">
          Return to the observatory
        </Link>
      </p>
    </div>
  );
}
