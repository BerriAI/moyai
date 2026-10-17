import React, { memo } from "react"
import "./space-backdrop.css"

// Stable positions across renders, with no canvas render loop or random layout.
const stars = Array.from({ length: 140 }, (_, index) => {
  const sample = (salt: number) => {
    const value = Math.sin((index + 1) * salt) * 43758.5453
    return value - Math.floor(value)
  }
  return { left: `${sample(12.9898) * 100}%`, top: `${sample(78.233) * 100}%`,
    opacity: .15 + sample(41.421) * .45, width: index % 7 === 0 ? 2 : 1, height: index % 7 === 0 ? 2 : 1 }
})

export const SpaceBackdrop = memo(function SpaceBackdrop({ variant = "home" }: { variant?: "home" | "chat" }) {
  return <div className={`space-backdrop ${variant}-space`} aria-hidden="true">
    {stars.map((style, index) => <span key={index} className="space-star" style={style} />)}
    {variant === "home" && <><div className="home-orbit home-orbit-outer" /><div className="home-orbit" /></>}
  </div>
})
