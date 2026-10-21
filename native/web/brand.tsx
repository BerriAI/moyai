import React from "react"
import { PanelLeftClose, PanelLeftOpen } from "lucide-react"
import { Button } from "@/components/ui/button"
import markWhite from "./assets/moyai-mark-white.svg"
import markBlue from "./assets/moyai-mark-blue.svg"
import "./brand.css"

export function MoyaiBrand({ onClick }: { onClick?: React.MouseEventHandler<HTMLAnchorElement> }) {
  return <a href="#" className="moyai-brand" onClick={onClick}>
    <img className="moyai-brand-mark moyai-brand-mark-dark" src={markWhite} width={20} height={20} alt="" />
    <img className="moyai-brand-mark moyai-brand-mark-light" src={markBlue} width={20} height={20} alt="" />
    <span className="moyai-brand-name">Moyai <small>by LiteLLM</small></span>
  </a>
}

export function SidebarToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? "Open sidebar" : "Close sidebar"
  return <Button className="sidebar-toggle" variant="ghost" size="icon-sm" aria-label={label} title={label} aria-expanded={!collapsed} onClick={onToggle}>
    {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
  </Button>
}
