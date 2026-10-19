/** The first 10 characters of a commit or blob SHA, as shown in lists. */
export function shortSha(sha: string): string {
  return sha.slice(0, 10);
}

const TIMESTAMP = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : TIMESTAMP.format(date);
}

export function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count.toLocaleString()} ${count === 1 ? singular : pluralForm}`;
}

/** "github.com/owner/name" -> its parts, or null when it is not exactly three non-empty parts. */
export function parseSlug(slug: string | null): { host: string; owner: string; name: string } | null {
  if (!slug) return null;
  const parts = slug.split("/");
  if (parts.length !== 3 || parts.some((part) => part === "")) return null;
  const [host, owner, name] = parts as [string, string, string];
  return { host, owner, name };
}
