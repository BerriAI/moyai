import React from "react"
import "./brand.css"

export function MoyaiBrand({ onClick }: { onClick?: React.MouseEventHandler<HTMLAnchorElement> }) {
  return <a href="#" className="moyai-brand" onClick={onClick}><span>Moyai</span><small>LiteLLM</small></a>
}
