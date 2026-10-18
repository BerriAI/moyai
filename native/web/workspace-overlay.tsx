import React, { forwardRef, useEffect, useSyncExternalStore } from "react"
import { Dialog as DialogPrimitive } from "radix-ui"
import { FolderCode } from "lucide-react"
import { Dialog, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import "./workspace-overlay.css"

// Matches the existing workspace breakpoint: the resource panel covers chat
// below this width. Geometry still comes entirely from the parent grid.
const compactQuery = "(max-width: 1000px)"
const compactSnapshot = () => window.matchMedia(compactQuery).matches
const serverSnapshot = () => false
function subscribeCompact(change: () => void) {
  const query = window.matchMedia(compactQuery)
  query.addEventListener("change", change)
  return () => query.removeEventListener("change", change)
}

type WorkspaceOverlayProps = {
  open: boolean
  onOpenChange: (open: boolean) => void
  panelVisible?: boolean
  panelExpanded?: boolean
  children: React.ReactNode
}

/** Wrap the workspace grid; this shared Radix root adds no layout element. */
export function WorkspaceOverlay({
  open, onOpenChange, panelVisible = false, panelExpanded = false, children,
}: WorkspaceOverlayProps) {
  const compact = useSyncExternalStore(subscribeCompact, compactSnapshot, serverSnapshot)
  const covered = panelExpanded || (compact && panelVisible)
  useEffect(() => {
    if (open && covered) onOpenChange(false)
  }, [open, covered, onOpenChange])
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if (covered || event.defaultPrevented || event.isComposing || event.repeat || event.altKey
        || !(event.metaKey || event.ctrlKey) || event.key !== ".") return
      if (document.querySelector('[data-slot="dialog-content"][data-state="open"]')) return
      event.preventDefault()
      onOpenChange(!open)
    }
    window.addEventListener("keydown", shortcut)
    return () => window.removeEventListener("keydown", shortcut)
  }, [open, covered, onOpenChange])
  return <Dialog modal={false} open={open && !covered} onOpenChange={onOpenChange}>{children}</Dialog>
}

export const WorkspaceOverlayTrigger = DialogTrigger

/** The supplied orbit-key design, backed by the shared accessible Button. */
export const WorkspaceKey = forwardRef<HTMLButtonElement, React.ComponentProps<typeof Button> & { open: boolean; alert?: boolean }>(
  function WorkspaceKey({ open, alert = false, className, ...props }, ref) {
    return <Button {...props} ref={ref} type="button" variant="ghost" size="icon-sm"
      className={cn("workspace-toggle workspace-orbit-key", className)} data-open={open}
      title={alert ? "Workspace · failed work · ⌘." : "Workspace · ⌘."}
      aria-description={alert ? "Workspace contains failed work" : undefined} aria-keyshortcuts="Meta+. Control+.">
      <FolderCode size={16} aria-hidden="true" />
      {alert && <span className="workspace-orbit-alert" aria-hidden="true" />}
    </Button>
  },
)

type ContentProps = Omit<React.ComponentProps<typeof DialogPrimitive.Content>, "asChild" | "forceMount"> & {
  label?: string
}

/**
 * Place in chat-layout alongside chat-content. Absolute placement never reflows chat.
 * Keep this component mounted while closed so Radix can finish its exit motion.
 *
 * The shared DialogContent adds a viewport overlay/centered portal. Using its
 * underlying Content inline retains Radix focus, dismissal and Presence while
 * making the main chat grid area the containing/clipping block.
 */
export function WorkspaceOverlayContent({
  children, className, label = "Workspace resources", ...props
}: ContentProps) {
  return <div className="workspace-overlay-host">
    <DialogPrimitive.Content
      {...props}
      data-slot="workspace-overlay-content"
      className={cn("workspace-overlay-content", className)}
      aria-describedby={undefined}
    >
      <DialogTitle className="workspace-overlay-label">{label}</DialogTitle>
      {children}
    </DialogPrimitive.Content>
  </div>
}
