/** Something shared into Celius from another app (S-11), stashed by the service worker. */
export interface Shared {
  id: string;
  text: string;
  files: File[];
}

const CACHE = "celius-share";

/** Take what was shared (once): reads and deletes the service worker's copy. */
export async function takeShared(): Promise<Shared | null> {
  const params = new URLSearchParams(location.search);
  const id = params.get("share");
  if (!id || !/^[0-9a-f-]{36}$/.test(id) || !("caches" in window)) return null;
  params.delete("share");
  const rest = params.toString();
  history.replaceState(null, "", `${location.pathname}${rest ? `?${rest}` : ""}${location.hash}`);

  const cache = await caches.open(CACHE);
  const meta = await cache.match(`/shared/${id}/meta`);
  if (!meta) return null;
  const { text, files: count } = (await meta.json()) as { text: string; files: number };
  const files: File[] = [];
  for (let index = 0; index < count; index++) {
    const response = await cache.match(`/shared/${id}/file/${index}`);
    if (!response) continue;
    const name = decodeURIComponent(response.headers.get("X-Name") ?? `shared-${index}`);
    files.push(new File([await response.blob()], name, { type: response.headers.get("Content-Type") ?? "" }));
    await cache.delete(`/shared/${id}/file/${index}`);
  }
  await cache.delete(`/shared/${id}/meta`);
  return { id, text, files };
}
