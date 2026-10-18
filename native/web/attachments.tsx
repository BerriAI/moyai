import React, { useEffect, useRef, useState } from "react"
import { FileText, LoaderCircle, Paperclip, Upload, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { apiRequest } from "./api"
import { isFileDrag, isInlineImage, receiveFileDrop } from "./attachment-files"
import { AttachmentUploads, type DraftFile, type UploadedFile } from "./attachment-uploads"
import "./attachments.css"

export type { UploadedFile } from "./attachment-uploads"

async function uploadFile(file: File, signal: AbortSignal): Promise<UploadedFile> {
  return apiRequest<UploadedFile>(`/uploads?name=${encodeURIComponent(file.name || "clipboard.png")}`, {
    method: "POST", headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file, signal,
  })
}

export function useAttachments(onError: (message: string) => void) {
  const [files, setFiles] = useState<DraftFile[]>([])
  const queue = useRef<AttachmentUploads | null>(null)
  if (!queue.current) queue.current = new AttachmentUploads(setFiles, uploadFile)
  useEffect(() => {
    queue.current!.activate()
    return () => queue.current!.dispose()
  }, [])

  async function add(files: File[]) {
    try { await queue.current!.add(files) } catch (error) { onError(String(error)) }
  }

  function paste(event: React.ClipboardEvent<HTMLElement>) {
    const files = Array.from(event.clipboardData.items)
      .filter(item => item.kind === "file" && item.type.startsWith("image/"))
      .map(item => item.getAsFile()).filter((file): file is File => file !== null)
    if (files.length) { event.preventDefault(); void add(files) }
  }

  return { files, uploading: files.some(file => file.uploadState === "uploading"),
    hasErrors: files.some(file => file.uploadState === "failed"),
    readyFiles: files.filter(file => file.uploadState === "ready").map(({ id, name, media_type, size }) => ({ id, name, media_type, size })),
    add, paste, clear: () => queue.current!.clear(),
    remove: (id: string) => queue.current!.remove(id), retry: (id: string) => queue.current!.retry(id) }
}

export function AttachmentPreviews({ files, remove, retry, draft = false }: { files: DraftFile[]; remove?: (id: string) => void; retry?: (id: string) => void; draft?: boolean }) {
  if (!files.length) return null
  return <div className="image-previews" aria-label="Attached files">{files.map(file => <div className={`image-preview${isInlineImage(file.media_type) ? "" : " file-preview"}${file.uploadState === "failed" ? " upload-failed" : ""}`} key={file.localId ?? file.id} aria-busy={file.uploadState === "uploading"}>
    {draft ? <div>{preview(file)}</div> : <a href={`/api/uploads/${file.id}`}>{preview(file)}</a>}
    {file.uploadState === "uploading" && <span className="attachment-upload-status" role="status"><LoaderCircle size={13} className="upload-spinner" />Uploading…</span>}
    {file.uploadState === "failed" && <div className="attachment-upload-error"><span role="alert">{file.error || "Upload failed."}</span>{retry && <Button type="button" variant="ghost" size="sm" aria-label={`Retry ${file.name}`} onClick={() => retry(file.id)}>Retry</Button>}</div>}
    {remove && <Button type="button" size="icon-sm" variant="secondary" aria-label={`Remove ${file.name}`} onClick={() => remove(file.id)}><X size={13} /></Button>}
  </div>)}</div>
}

function preview(file: DraftFile) {
  return <>{isInlineImage(file.media_type) ? <img src={file.previewUrl || `/api/uploads/${file.id}`} alt={file.name} /> : <FileText size={28} aria-hidden="true" />}
    <span title={file.name}>{file.name}</span>
    {!isInlineImage(file.media_type) && <small>{file.size < 1024 ? `${file.size} B` : `${(file.size / 1024).toFixed(1)} KB`}</small>}
  </>
}

export function useFileDrop(add: (files: File[]) => Promise<void>, onError: (message: string) => void, disabled = false) {
  const [dragging, setDragging] = useState(false)
  const depth = useRef(0)
  const reset = () => { depth.current = 0; setDragging(false) }
  useEffect(() => {
    // Dropping outside a composer must not navigate away from an unsent draft.
    const preventNavigation = (event: DragEvent) => { if (event.dataTransfer && isFileDrag(event.dataTransfer)) event.preventDefault() }
    const finish = (event: DragEvent) => { preventNavigation(event); reset() }
    window.addEventListener("dragover", preventNavigation)
    window.addEventListener("drop", finish)
    window.addEventListener("dragend", reset)
    window.addEventListener("blur", reset)
    return () => {
      window.removeEventListener("dragover", preventNavigation)
      window.removeEventListener("drop", finish)
      window.removeEventListener("dragend", reset)
      window.removeEventListener("blur", reset)
    }
  }, [])
  useEffect(() => { if (disabled) reset() }, [disabled])
  return { dragging, handlers: {
    onDragEnter(event: React.DragEvent<HTMLElement>) {
      if (!isFileDrag(event.dataTransfer)) return
      event.preventDefault(); event.stopPropagation()
      depth.current += 1
      if (!disabled) setDragging(true)
    },
    onDragOver(event: React.DragEvent<HTMLElement>) {
      if (!isFileDrag(event.dataTransfer)) return
      event.preventDefault(); event.stopPropagation()
      event.dataTransfer.dropEffect = disabled ? "none" : "copy"
    },
    onDragLeave(event: React.DragEvent<HTMLElement>) {
      if (!isFileDrag(event.dataTransfer)) return
      event.preventDefault(); event.stopPropagation()
      depth.current = Math.max(0, depth.current - 1)
      if (!depth.current) setDragging(false)
    },
    onDrop(event: React.DragEvent<HTMLElement>) {
      if (receiveFileDrop(event, add, onError, disabled)) reset()
    },
  } }
}

export function FileDropOverlay() {
  return <div className="file-drop-overlay" role="status"><Upload size={28} /><strong>Drop files to attach</strong><span>Up to 8 files · 20 MB each · Images up to 8 MB</span></div>
}

export function AttachButton({ add, disabled, compact = false }: { add: (files: File[]) => Promise<void>; disabled?: boolean; compact?: boolean }) {
  const input = useRef<HTMLInputElement>(null)
  return <>
    <input ref={input} className="visually-hidden" type="file" multiple disabled={disabled} aria-label="Attach files" onChange={event => { void add(Array.from(event.target.files ?? [])); event.target.value = "" }} />
    <Button type="button" variant="ghost" size={compact ? "icon-sm" : "sm"} aria-label="Attach" title="Attach files" disabled={disabled} onClick={() => input.current?.click()}><Paperclip size={16} />{!compact && <span className="composer-optional-label">Attach</span>}</Button>
  </>
}
