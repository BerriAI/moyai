import React, { useEffect, useSyncExternalStore } from "react"
import { Dialog as DialogPrimitive } from "radix-ui"
import { Dialog, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
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
  return <Dialog modal={false} open={open && !covered} onOpenChange={onOpenChange}>{children}</Dialog>
}

export const WorkspaceOverlayTrigger = DialogTrigger

type ContentProps = Omit<React.ComponentProps<typeof DialogPrimitive.Content>, "asChild" | "forceMount"> & {
  label?: string
}

/**
 * Place directly inside the workspace grid, alongside chat and PanelTabs.
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
