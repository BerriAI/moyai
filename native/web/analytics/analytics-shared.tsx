import React, { useMemo, useRef, useState } from "react"
import parse, { Element } from "html-react-parser"
import { CalendarDays, Check, ChevronDown } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { analyticsChart, analyticsLegend, analyticsMetrics } from "./analytics-render.js"
import type { ChartOptions, ChartRow, ChartSeries } from "./analytics-render.js"
import { analyticsPresets, customAnalyticsRange, presetAnalyticsRange, rangeDates, rangeLabel } from "./analytics-dates"
import type { AnalyticsRange } from "./analytics-dates"
import { dollars, finiteValue, spendCount } from "./analytics-format"
import "./analytics-shared.css"

export function AnalyticsDatePicker({ value, onChange, disabled = false }: {
  value: AnalyticsRange; onChange: (range: AnalyticsRange) => void; disabled?: boolean
}) {
  const [open, setOpen] = useState(false), [custom, setCustom] = useState(value.preset === "custom")
  const [dates, setDates] = useState(() => rangeDates(value)), [error, setError] = useState("")
  const startInput = useRef<HTMLInputElement>(null)
  const commit = (range: AnalyticsRange) => { onChange(range); setOpen(false) }
  return <Popover open={open} onOpenChange={next => {
    if (next) { setDates(rangeDates(value)); setCustom(value.preset === "custom"); setError("") }
    setOpen(next)
  }}>
    <PopoverTrigger asChild><Button className="native-analytics-date-trigger" type="button" variant="outline" disabled={disabled} aria-label={`Date range: ${rangeLabel(value)}`} title={`${value.start} to ${value.end}, exclusive · UTC`}><CalendarDays size={16} /><span>{rangeLabel(value)}</span><ChevronDown size={14} /></Button></PopoverTrigger>
    <PopoverContent className="native-analytics-range" align="end" collisionPadding={12} aria-label="Choose report dates">
      <div className="native-analytics-presets" role="group" aria-label="Date presets">{analyticsPresets.map(item => <Button key={item.value} variant="ghost" type="button" aria-pressed={item.value === "custom" ? custom : !custom && value.preset === item.value} onClick={() => {
        if (item.value === "custom") { setCustom(true); requestAnimationFrame(() => startInput.current?.focus()); return }
        commit(presetAnalyticsRange(item.value))
      }}><span>{item.label}</span>{(item.value === "custom" ? custom : !custom && value.preset === item.value) && <Check size={15} aria-hidden="true" />}</Button>)}</div>
      {custom && <form className="native-analytics-custom-dates" onSubmit={event => {
        event.preventDefault()
        try { commit(customAnalyticsRange(dates.start, dates.through)) }
        catch (error) { setError(error instanceof Error ? error.message : "Choose valid dates.") }
      }}>
        <label>From<Input ref={startInput} type="date" required value={dates.start} onChange={event => setDates(previous => ({ ...previous, start: event.target.value }))} /></label>
        <label>Through<Input type="date" required min={dates.start} value={dates.through} onChange={event => setDates(previous => ({ ...previous, through: event.target.value }))} /></label>
        <p>Both dates included · UTC · up to 366 days</p>
        {error && <p className="native-analytics-range-error" role="alert">{error}</p>}
        <Button type="submit">Apply dates</Button>
      </form>}
      {!custom && <p className="native-analytics-date-note">24 hours is a rolling window. Day presets include today in UTC.</p>}
    </PopoverContent>
  </Popover>
}

export function AnalyticsMetrics({ items }: { items: [string, string, string?][] }) {
  // The existing helper escapes every label, value, and note.
  return <>{parse(analyticsMetrics(items))}</>
}

export function ReportTable({ label, columns, children }: { label: string; columns: React.ReactNode[]; children: React.ReactNode }) {
  return <div className="analytics-table-frame" role="region" aria-label={label} tabIndex={0}><Table>
    <TableHeader><TableRow>{columns.map((column, index) => <TableHead scope="col" key={index}>{column}</TableHead>)}</TableRow></TableHeader><TableBody>{children}</TableBody>
  </Table></div>
}

export function AnalyticsLegend({ series, cumulative = false }: { series: ChartSeries[]; cumulative?: boolean }) {
  return <>{parse(analyticsLegend(series, cumulative))}</>
}

function segments(points: string[], known: boolean[]): string[][] {
  const result: string[][] = []
  let current: string[] = []
  points.forEach((point, index) => {
    if (known[index]) current.push(point)
    else if (current.length) { result.push(current); current = [] }
  })
  if (current.length) result.push(current)
  return result
}

/**
 * Retain the established SVG renderer, but remove its zero placeholders for
 * unavailable observations. Gaps split lines instead of implying free usage.
 */
export function renderAnalyticsChart(rows: ChartRow[], series: ChartSeries[], options: ChartOptions): React.ReactNode {
  const known = rows.map(row => series.map(item => finiteValue(row[item.key])))
  const anyKnown = known.map((row, index) => row.some(Boolean) || Boolean(options.line && finiteValue(rows[index][options.line.key])))
  if (!series.length || !anyKnown.some(Boolean)) return <p className="analytics-empty-chart">No recorded values for this chart. Unavailable values are not plotted as zero.</p>
  const normalized = rows.map(row => {
    const next = { ...row }
    for (const item of [...series, ...(options.line ? [options.line] : [])]) next[item.key] = finiteValue(row[item.key]) ? row[item.key] : 0
    return next
  })
  let bar = 0, point = 0
  return parse(analyticsChart(normalized, series, options), {
    replace(node) {
      if (!(node instanceof Element)) return
      const classes = node.attribs.class ?? ""
      if (node.name === "rect" && /(?:^|\s)analytics-color-\d/.test(classes)) {
        const row = Math.floor(bar / series.length), column = bar++ % series.length
        if (!known[row]?.[column]) return <></>
      }
      if (node.name === "circle" && classes === "analytics-point" && !known[point++]?.[0]) return <></>
      if (node.name === "polygon" && classes === "analytics-area") {
        const points = node.attribs.points.trim().split(/\s+/), baseline = points[0].split(",")[1]
        return <g>{segments(points.slice(1, -1), known.map(row => row[0])).map((part, index) => <polygon key={index} className={classes} points={`${part[0].split(",")[0]},${baseline} ${part.join(" ")} ${part.at(-1)!.split(",")[0]},${baseline}`} />)}</g>
      }
      if (node.name === "polyline" && ["analytics-trend", "analytics-average", "analytics-cumulative"].includes(classes)) {
        const available = classes === "analytics-average" && options.line
          ? rows.map(row => finiteValue(row[options.line!.key]))
          : classes === "analytics-cumulative" ? known.map(row => row.some(Boolean)) : known.map(row => row[0])
        const parts = segments(node.attribs.points.trim().split(/\s+/), available)
        const partial = classes === "analytics-cumulative" && rows.some(row => row.partial)
        return <g>{parts.map((part, index) => part.length > 1
          ? <polyline key={index} className={`${classes}${partial ? " analytics-cumulative-partial" : ""}`} points={part.join(" ")} />
          : classes === "analytics-trend" ? null : <circle key={index} className={classes === "analytics-average" ? "analytics-average-point" : "analytics-cumulative-point"} cx={Number(part[0].split(",")[0])} cy={Number(part[0].split(",")[1])} r={3} />)}</g>
      }
    },
  })
}

export function AnalyticsChart({ rows, series, title, money = false, area = false, cumulative = false, line, cumulativeKey }: ChartOptions & {
  rows: ChartRow[]; series: ChartSeries[]; cumulativeKey?: string
}) {
  const markup = useMemo(() => renderAnalyticsChart(rows, series, { title, money, area, cumulative, line }), [rows, series, title, money, area, cumulative, line])
  const cells = [...series, ...(line ? [line] : [])]
  const missing = rows.some(row => cells.some(item => !finiteValue(row[item.key])))
  const format = money ? dollars : spendCount
  return <div className="native-analytics-chart">
    {markup}
    {missing && <p className="analytics-footnote">Unavailable observations are omitted. Gaps do not represent zero values.</p>}
    {money && cumulative && <p className="analytics-footnote">Cumulative values show the reported subtotal. Missing costs are excluded; dashed lines indicate partial coverage.</p>}
    <details className="analytics-breakdown"><summary>View daily values</summary><div className="analytics-table-frame" role="region" aria-label={`${title}: daily values`} tabIndex={0}><Table>
      <TableHeader><TableRow><TableHead scope="col">Date (UTC)</TableHead>{cells.map(item => <TableHead scope="col" key={item.key}>{item.label}</TableHead>)}{cumulativeKey && <TableHead scope="col">Cumulative reported cost</TableHead>}</TableRow></TableHeader>
      <TableBody>{rows.map((row, index) => <TableRow key={`${row.date}:${index}`}><TableCell>{row.date}</TableCell>{cells.map(item => <TableCell key={item.key}>{format(row[item.key])}</TableCell>)}{cumulativeKey && <TableCell>{dollars(row[cumulativeKey])}</TableCell>}</TableRow>)}</TableBody>
    </Table></div></details>
  </div>
}
