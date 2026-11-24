import React, { createContext, useContext, useState } from "react"
import { ExternalLink, ImageOff, Maximize2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import "./message-image.css"

const InsideLink = createContext(false)

/** A linked Markdown image keeps its destination, without a nested button. */
export function MessageImageLink(props: React.ComponentProps<"a">) {
  return <InsideLink.Provider value><a {...props} /></InsideLink.Provider>
}

export function MessageImage({ src, alt, title }: { src?: string; alt?: string; title?: string }) {
  const linked = useContext(InsideLink)
  const [failed, setFailed] = useState(false)
  const label = alt || "Image"
  const unavailable = <span className="message-image-unavailable"><ImageOff size={20} aria-hidden="true" />
    <span>{label}<small>Image unavailable</small></span>
  </span>
  const preview = failed || !src ? unavailable : <img className="message-image-preview" src={src}
    alt={alt || "Image"} title={title} decoding="async" onError={() => setFailed(true)} />

  if (linked || !src) return preview
  return <Dialog>
    <DialogTrigger asChild>
      <Button type="button" variant="ghost" className="message-image" aria-label={`Enlarge image: ${label}`}>
        {preview}
        {!failed && <span className="message-image-expand" aria-hidden="true"><Maximize2 size={14} /></span>}
      </Button>
    </DialogTrigger>
    <DialogContent className="message-image-dialog" aria-describedby={undefined}>
      <div className="message-image-toolbar">
        <DialogTitle className="message-image-title" title={label}>{label}</DialogTitle>
        <Button asChild variant="ghost" className="message-image-original">
          <a href={src} target="_blank" rel="noreferrer" aria-label="Open original"><ExternalLink size={16} aria-hidden="true" /><span>Open original</span></a>
        </Button>
      </div>
      <div className="message-image-stage">
        {failed ? unavailable : <img src={src} alt={alt || "Image"} onError={() => setFailed(true)} />}
      </div>
    </DialogContent>
  </Dialog>
}
