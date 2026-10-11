import * as React from "react"
import { CheckIcon, ChevronDownIcon, ChevronUpIcon } from "lucide-react"
import { Select as SelectPrimitive } from "radix-ui"
import { cn } from "@/lib/utils"

function Select(props: React.ComponentProps<typeof SelectPrimitive.Root>) {
  return <SelectPrimitive.Root {...props} />
}

function SelectTrigger({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return <SelectPrimitive.Trigger data-slot="select-trigger" className={cn(
    "flex w-full items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-2 text-sm shadow-xs outline-none disabled:cursor-not-allowed disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0",
    className,
  )} {...props}>
    {children}
    <SelectPrimitive.Icon asChild><ChevronDownIcon data-slot="native-select-icon" className="size-4" aria-hidden="true" /></SelectPrimitive.Icon>
  </SelectPrimitive.Trigger>
}

function SelectContent({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Content>) {
  return <SelectPrimitive.Portal>
    <SelectPrimitive.Content data-slot="select-content" position="popper" align="start" sideOffset={4} collisionPadding={8}
      className={cn("relative z-50 overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md", className)} {...props}>
      <SelectPrimitive.ScrollUpButton data-slot="select-scroll-up-button" className="flex items-center justify-center py-1"><ChevronUpIcon className="size-4" /></SelectPrimitive.ScrollUpButton>
      <SelectPrimitive.Viewport data-slot="select-viewport" className="p-1">{children}</SelectPrimitive.Viewport>
      <SelectPrimitive.ScrollDownButton data-slot="select-scroll-down-button" className="flex items-center justify-center py-1"><ChevronDownIcon className="size-4" /></SelectPrimitive.ScrollDownButton>
    </SelectPrimitive.Content>
  </SelectPrimitive.Portal>
}

function SelectItem({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return <SelectPrimitive.Item data-slot="select-item" className={cn(
    "relative flex w-full cursor-default items-center gap-2 rounded-sm py-1.5 pr-8 pl-2 text-sm outline-none select-none focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
    className,
  )} {...props}>
    <span data-slot="select-item-indicator" className="absolute right-2 flex size-4 items-center justify-center">
      <SelectPrimitive.ItemIndicator><CheckIcon className="size-4" /></SelectPrimitive.ItemIndicator>
    </span>
    <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
  </SelectPrimitive.Item>
}

const SelectValue = SelectPrimitive.Value
const SelectGroup = SelectPrimitive.Group
function SelectLabel(props: React.ComponentProps<typeof SelectPrimitive.Label>) {
  return <SelectPrimitive.Label data-slot="select-label" className="px-2 py-1.5 text-xs text-muted-foreground" {...props} />
}

export { Select, SelectTrigger, SelectContent, SelectItem, SelectValue, SelectGroup, SelectLabel }
