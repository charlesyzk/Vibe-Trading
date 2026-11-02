/** Preserve explicit operator identity; the backend validates the token. */
export function applyBackendAuthorization(headers: Record<string, string | string[]>, sharedKey: string, requestUrl: string, desktopPaperToken?: string): void {
  const keys = Object.keys(headers).filter((key) => key.toLowerCase() === "authorization");
  const existing = keys.length === 1 ? headers[keys[0]] : undefined;
  for (const key of keys) delete headers[key];
  const paperRoute = new URL(requestUrl).pathname.startsWith("/api/execution/paper/");
  headers.Authorization = paperRoute && typeof existing === "string" && /^Bearer vtpo_[A-Za-z0-9_-]+$/.test(existing)
    ? existing : paperRoute && desktopPaperToken && /^vtdp_[A-Za-z0-9_-]{40,128}$/.test(desktopPaperToken)
      ? `Bearer ${desktopPaperToken}` : `Bearer ${sharedKey}`;
}
