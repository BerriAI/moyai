import { attachmentError, isInlineImage } from "./attachment-files"

export type UploadedFile = { id: string; name: string; media_type: string; size: number }
export type DraftFile = UploadedFile & {
  localId?: string
  uploadState?: "uploading" | "failed" | "ready"
  previewUrl?: string
  error?: string
}
type Upload = (file: File, signal: AbortSignal) => Promise<UploadedFile>
type Job = { id: string; file: File; preview?: string; controller?: AbortController; finish: () => void }
type PreviewUrls = { create(file: File): string; revoke(url: string): void }

/** Ephemeral composer uploads. A local card exists before any network request. */
export class AttachmentUploads {
  files: DraftFile[] = []
  private jobs = new Map<string, Job>()
  private queue: Job[] = []
  private active = 0
  private disposed = false

  constructor(
    private changed: (files: DraftFile[]) => void,
    private upload: Upload,
    private urls: PreviewUrls = { create: file => URL.createObjectURL(file), revoke: url => URL.revokeObjectURL(url) },
  ) {}

  activate() { this.disposed = false }
  dispose() { this.disposed = true; this.clear(false) }

  private publish(files: DraftFile[]) {
    this.files = files
    if (!this.disposed) this.changed(files)
  }

  add(files: File[]): Promise<void> {
    if (this.disposed || !files.length) return Promise.resolve()
    const error = attachmentError(files, this.files.length)
    if (error) return Promise.reject(new Error(error))
    const completions: Promise<void>[] = []
    const drafts = files.map(file => {
      const id = `local:${crypto.randomUUID()}`
      let finish = () => {}
      completions.push(new Promise<void>(resolve => { finish = resolve }))
      const preview = isInlineImage(file.type) ? this.urls.create(file) : undefined
      const job: Job = { id, file, preview, finish }
      this.jobs.set(id, job); this.queue.push(job)
      return { id, localId: id, name: file.name, size: file.size, media_type: file.type,
        uploadState: "uploading" as const, previewUrl: preview }
    })
    this.publish([...this.files, ...drafts])
    this.pump()
    return Promise.all(completions).then(() => {})
  }

  retry(id: string) {
    const file = this.files.find(item => item.id === id || item.localId === id)
    const job = file?.localId ? this.jobs.get(file.localId) : undefined
    if (this.disposed || !file || !job || file.uploadState !== "failed") return
    this.publish(this.files.map(item => item === file ? { ...item, uploadState: "uploading", error: undefined } : item))
    this.queue.push(job); this.pump()
  }

  remove(id: string) {
    const file = this.files.find(item => item.id === id || item.localId === id)
    const job = file?.localId ? this.jobs.get(file.localId) : undefined
    if (job) {
      this.jobs.delete(job.id); job.controller?.abort(); job.finish()
      if (job.preview) this.urls.revoke(job.preview)
    }
    this.publish(this.files.filter(item => item !== file))
  }

  clear(notify = true) {
    for (const job of this.jobs.values()) {
      job.controller?.abort(); job.finish()
      if (job.preview) this.urls.revoke(job.preview)
    }
    this.jobs.clear(); this.queue = []; this.files = []
    if (notify && !this.disposed) this.changed([])
  }

  private pump() {
    while (!this.disposed && this.active < 3 && this.queue.length) {
      const job = this.queue.shift()!
      if (this.jobs.get(job.id) !== job) continue
      const controller = new AbortController()
      job.controller = controller
      this.active += 1
      const current = () => !this.disposed && !controller.signal.aborted && this.jobs.get(job.id) === job
      void this.upload(job.file, controller.signal).then(value => {
        if (current()) this.publish(this.files.map(file => file.localId === job.id
          ? { ...value, localId: job.id, previewUrl: job.preview, uploadState: "ready" } : file))
      }, error => {
        if (current()) this.publish(this.files.map(file => file.localId === job.id
          ? { ...file, uploadState: "failed", error: error instanceof Error ? error.message : "Upload failed." } : file))
      }).finally(() => {
        this.active -= 1; job.finish(); this.pump()
      })
    }
  }
}
