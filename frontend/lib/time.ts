export function relativeDate(value: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60_000))
  const hours = Math.floor(minutes / 60), days = Math.floor(hours / 24), months = Math.floor(days / 30)
  return minutes < 1 ? "Just now" : minutes < 60 ? `${minutes}m ago` : hours < 24 ? `${hours} hour${hours === 1 ? "" : "s"} ago` : days < 30 ? `${days} day${days === 1 ? "" : "s"} ago` : months < 2 ? "last month" : months < 12 ? `${months} months ago` : new Date(value).toLocaleDateString()
}
