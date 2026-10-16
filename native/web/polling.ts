/** One cancellable read at a time; hidden tabs resume with a fresh read. */
export function pollVisible(
  read: (signal: AbortSignal) => Promise<unknown>,
  delay: () => number,
  onError: (reason: unknown) => void,
) {
  let stopped = false, generation = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  const cancel = () => { generation++; clearTimeout(timer); controller?.abort() }
  const poll = async () => {
    if (stopped || document.hidden) return
    const version = ++generation, request = new AbortController()
    controller = request
    try { await read(request.signal) }
    catch (reason) { if (!request.signal.aborted && version === generation) onError(reason) }
    if (!stopped && !document.hidden && version === generation) timer = setTimeout(poll, delay())
  }
  const visibility = () => { cancel(); if (!document.hidden) void poll() }
  document.addEventListener("visibilitychange", visibility)
  void poll()
  return () => { stopped = true; cancel(); document.removeEventListener("visibilitychange", visibility) }
}

export const activeSession = (status?: string) =>
  Boolean(status && ["queued", "starting", "running", "saving", "stopping"].includes(status))
