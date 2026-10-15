import React, { useEffect, useRef, useState } from "react"
import { ImagePlus, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { csrfToken } from "./api"

export type UploadedImage = { id: string; name: string; media_type: string; size: number }

export function useImages(onError: (message: string) => void) {
  const [images, setImages] = useState<UploadedImage[]>([])
  const [uploading, setUploading] = useState(false)
  const pending = useRef(false)
  const current = useRef(images)
  current.current = images
  const abort = useRef(new AbortController())
  useEffect(() => () => abort.current.abort(), [])

  async function add(files: File[]) {
    if (pending.current) { onError("Wait for the current images to finish uploading."); return }
    const accepted = files.filter(file => file.type.startsWith("image/"))
    if (!accepted.length) return
    if (current.current.length + accepted.length > 8) { onError("Attach up to 8 images per message."); return }
    pending.current = true; setUploading(true)
    try {
      for (const file of accepted) {
        if (file.size > 8 * 1024 * 1024) throw new Error("Images must be 8 MB or smaller.")
        const response = await fetch(`/api/uploads?name=${encodeURIComponent(file.name || "clipboard.png")}`, {
          method: "POST", headers: { "X-Moyai-CSRF": csrfToken(), "Content-Type": file.type },
          body: file, signal: abort.current.signal,
        })
        const result = await response.json()
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Image upload failed.")
        if (!abort.current.signal.aborted) setImages(previous => [...previous, result as UploadedImage])
      }
    } catch (error) {
      if (!abort.current.signal.aborted) onError(String(error))
    } finally {
      pending.current = false
      if (!abort.current.signal.aborted) setUploading(false)
    }
  }

  function paste(event: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(event.clipboardData.items)
      .filter(item => item.kind === "file" && item.type.startsWith("image/"))
      .map(item => item.getAsFile()).filter((file): file is File => file !== null)
    if (files.length) { event.preventDefault(); void add(files) }
  }

  return { images, uploading, add, paste, clear: () => setImages([]),
    remove: (id: string) => setImages(previous => previous.filter(image => image.id !== id)) }
}

export function ImagePreviews({ images, remove }: { images: UploadedImage[]; remove?: (id: string) => void }) {
  if (!images.length) return null
  return <div className="image-previews">{images.map(image => <div className="image-preview" key={image.id}>
    <a href={`/api/uploads/${image.id}`} target="_blank" rel="noreferrer">
      <img src={`/api/uploads/${image.id}`} alt={image.name} /><span>{image.name}</span>
    </a>
    {remove && <Button type="button" size="icon-sm" variant="secondary" aria-label={`Remove ${image.name}`} onClick={() => remove(image.id)}><X size={13} /></Button>}
  </div>)}</div>
}

export function AttachButton({ add, disabled }: { add: (files: File[]) => Promise<void>; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null)
  return <>
    <input ref={input} className="visually-hidden" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple aria-label="Attach images" onChange={event => { void add(Array.from(event.target.files ?? [])); event.target.value = "" }} />
    <Button type="button" variant="ghost" size="sm" disabled={disabled} onClick={() => input.current?.click()}><ImagePlus size={16} />Attach</Button>
  </>
}
