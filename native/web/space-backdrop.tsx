import React, { memo } from "react"
import "./space-backdrop.css"

export const SpaceBackdrop = memo(function SpaceBackdrop({ variant = "home" }: { variant?: "home" | "chat" }) {
  return <div className={`space-backdrop ${variant}-space`} aria-hidden="true" />
})
