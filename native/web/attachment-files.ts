export const MAX_ATTACHMENTS = 8
export const MAX_FILE_BYTES = 20 * 1024 * 1024
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024

export function isInlineImage(mediaType: string): boolean {
  return ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(mediaType)
}

export function attachmentError(files: Pick<File, "name" | "size" | "type">[], count: number): string | undefined {
  if (count + files.length > MAX_ATTACHMENTS) return "Attach up to 8 files per message."
  for (const file of files) {
    const image = isInlineImage(file.type) || /\.(png|jpe?g|gif|webp)$/i.test(file.name)
    if (file.size > (image ? MAX_IMAGE_BYTES : MAX_FILE_BYTES)) {
      return `${file.name || "This file"} is too large. ${image ? "Images must be 8 MB" : "Files must be 20 MB"} or smaller.`
    }
  }
}

export function isFileDrag(transfer: Pick<DataTransfer, "types">): boolean {
  return Array.from(transfer.types).includes("Files")
}

export function droppedFiles(transfer: Pick<DataTransfer, "files" | "items">): File[] {
  if (Array.from(transfer.items).some(item => item.kind === "file" && item.webkitGetAsEntry?.()?.isDirectory)) {
    throw new Error("Drop individual files. To attach a folder, zip it first.")
  }
  const files = Array.from(transfer.files)
  if (!files.length) throw new Error("No files could be read from this drop.")
  return files
}

type DropEvent = {
  dataTransfer: DataTransfer
  preventDefault(): void
  stopPropagation(): void
}

export function receiveFileDrop(
  event: DropEvent, add: (files: File[]) => Promise<void>, onError: (message: string) => void, disabled: boolean,
): boolean {
  if (!isFileDrag(event.dataTransfer)) return false
  event.preventDefault(); event.stopPropagation()
  if (disabled) {
    onError("Finish the current upload or message edit before attaching files.")
    return true
  }
  try { void add(droppedFiles(event.dataTransfer)).catch(error => onError(String(error))) }
  catch (error) { onError(String(error)) }
  return true
}
