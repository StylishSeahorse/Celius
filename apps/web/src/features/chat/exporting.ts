import { api, fileUrls } from "../../lib/api";

/** Queue an export, wait for the worker to build it, then download it. */
export async function runExport(body: Parameters<typeof api.createExport>[0]): Promise<void> {
  let job = await api.createExport(body);
  for (let delay = 400; job.status === "queued" || job.status === "running"; delay = Math.min(delay * 1.5, 3000)) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    job = await api.getExport(job.id);
  }
  if (job.status !== "completed" || !job.file) throw new Error(job.error ?? "The export failed");
  const link = document.createElement("a");
  link.href = fileUrls.download(job.file.id);
  link.download = job.file.filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
}
