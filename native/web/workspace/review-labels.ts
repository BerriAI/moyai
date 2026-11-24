const github = { name: "Pull request", plural: "pull requests", short: "PR", marker: "#" }
const gitlab = { name: "Merge request", plural: "merge requests", short: "MR", marker: "!" }

/** Presentation only; the server authorizes the provider, origin and project. */
export function reviewLabels(url: string) {
  try {
    if (/^\/[^/]+(?:\/[^/]+)+\/-\/merge_requests\/[1-9][0-9]*\/?$/.test(new URL(url).pathname)) return gitlab
  } catch { /* Preserve the existing GitHub label for an unavailable URL. */ }
  return github
}
