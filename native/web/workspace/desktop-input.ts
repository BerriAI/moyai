export type DesktopInput = { action: string; [key: string]: string | number | boolean }

/** Ordered input with a bounded scroll batch instead of a wheel-event backlog. */
export class DesktopInputQueue {
  private tail = Promise.resolve()
  private scrollDelta = 0
  private scrollPending = false

  constructor(
    private send: (input: DesktopInput) => Promise<unknown>,
    private allowed: () => boolean,
    private failed: (error: unknown) => void,
  ) {}

  enqueue(action: string, values: Record<string, string | number | boolean>) {
    if (!this.allowed()) return
    // A click/key supersedes movement left over from an earlier scroll gesture.
    this.scrollDelta = 0
    this.tail = this.tail.then(() => this.dispatch({ action, ...values }))
  }

  wheel(delta: number) {
    if (!this.allowed() || !Number.isFinite(delta)) return
    this.scrollDelta = Math.max(-800, Math.min(800, this.scrollDelta + delta))
    this.flushScroll()
  }

  private flushScroll() {
    if (this.scrollPending || !this.scrollDelta || !this.allowed()) return
    this.scrollPending = true
    this.tail = this.tail.then(async () => {
      const delta = this.scrollDelta
      this.scrollDelta = 0
      if (!delta) return
      await this.dispatch({
        action: "scroll", direction: delta < 0 ? "up" : "down",
        steps: Math.min(10, Math.max(1, Math.round(Math.abs(delta) / 80))),
      })
    }).finally(() => {
      this.scrollPending = false
      // At most one subsequent batch exists, behind any newer pointer/key input.
      this.flushScroll()
    })
  }

  private async dispatch(input: DesktopInput) {
    if (!this.allowed()) return
    try { await this.send(input) }
    catch (error) { this.scrollDelta = 0; this.failed(error) }
  }

  async drain() {
    this.scrollDelta = 0
    await this.tail
  }
}
