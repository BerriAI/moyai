import { Button } from "../components/ui/button"
import { Toaster } from "../components/ui/sonner"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../components/ui/tooltip"
import { FormSelect } from "../form-select"
import { optionFragment } from "../lib/options"

const scopeOptions = optionFragment([["mine", "My sessions"]])
const buttonClass = "h-auto whitespace-normal shrink"

function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <span data-icon={name} data-icon-size={size} />
}

function ShellButton({ title, children, ...props }: React.ComponentProps<typeof Button> & { title: string }) {
  return <Tooltip><TooltipTrigger asChild><Button {...props} className={`${buttonClass} ${props.className ?? ""}`} variant="ghost">{children}</Button></TooltipTrigger><TooltipContent sideOffset={6}>{title}</TooltipContent></Tooltip>
}

// The route-owned page host. Empty hosts belong to a controller; React pages
// pass children. A route change replaces the host, never shares it.
export function ContentHost({ children }: { children?: React.ReactNode }) {
  return <main id="content" tabIndex={0}>{children}</main>
}

// Rail and header hosts are filled by the controllers. React owns their shell,
// never their mutable children. Feature roots dispose themselves on exit.
export function AppShell({ children }: { children: React.ReactNode }) {
  return <TooltipProvider delayDuration={500}>
    <a className="skip-link" href="#content">Skip to content</a>
    <Button id="sidebar-scrim" className={`sidebar-scrim ${buttonClass}`} aria-label="Close sidebar" hidden />
    <aside className="rail" id="sidebar" aria-label="Workspace sidebar">
      <div className="rail-brand"><a className="brand" href="#tasks" aria-label="Moyai home"><img src="/static/favicon.svg?v=moyai-train-1" alt="" width={27} height={27} /><span className="brand-copy"><span>Moyai</span><small id="workspace-name">Workspace</small></span></a>
        <div className="rail-tools"><ShellButton title="Search sessions" id="search-sessions" className="icon-button" aria-label="Search sessions"><Icon name="search" /></ShellButton><ShellButton title="Hide sidebar" id="close-sidebar" className="icon-button" aria-label="Close sidebar"><Icon name="sidebar" /></ShellButton></div>
      </div>
      <Button variant="ghost" className={`new-task ${buttonClass}`} id="new-task"><span className="nav-icon"><Icon name="plus" size={17} /></span>New session<kbd>⌘⇧O</kbd></Button>
      <Button variant="ghost" id="command-menu" className={`nav-button ${buttonClass}`} type="button" aria-haspopup="dialog" aria-controls="command-palette" aria-expanded={false}><span className="nav-icon"><Icon name="search" size={17} /></span>Search and commands<kbd>⌘K</kbd></Button>
      <nav className="workspace-nav rail-links" aria-label="Workspace">
        <Button variant="ghost" className={`nav-button ${buttonClass}`} data-view="automations"><span className="nav-icon"><Icon name="clock" size={17} /></span>Automations</Button>
        <Button variant="ghost" className={`nav-button ${buttonClass}`} data-view="skills"><span className="nav-icon"><Icon name="book" size={17} /></span>Skills</Button>
        <Button variant="ghost" className={`nav-button ${buttonClass}`} data-view="connections"><span className="nav-icon"><Icon name="grid" size={17} /></span>Connections</Button>
      </nav>
      <div className="sessions-label"><FormSelect id="session-scope" aria-label="Filter sessions" disabled initialOptions={scopeOptions} /><span id="task-count">0</span><ShellButton title="Create a folder to organize sessions" id="new-folder" className="folder-add" type="button" aria-label="Add folder" aria-haspopup="dialog" aria-controls="session-folder-dialog"><Icon name="folder-plus" size={16} /></ShellButton></div>
      <nav id="session-list" className="session-list" aria-label="Recent sessions" />
      <nav id="settings-navigation" aria-label="Settings" hidden />
      <div className="rail-foot"><small className="rail-identity">Private workspace</small><div className="rail-account-actions"><ShellButton title="Settings" className="nav-button rail-settings" data-view="settings" aria-label="Settings"><span className="nav-icon"><Icon name="gear" size={16} /></span></ShellButton><ShellButton title="Sign out" id="logout" className="quiet rail-logout" type="button" aria-label="Sign out" hidden><Icon name="logout" size={16} /></ShellButton></div></div>
    </aside>
    <div className="workspace"><header className="topbar"><ShellButton title="Show sidebar" id="open-sidebar" className="icon-button" aria-label="Open sidebar" aria-expanded={false} aria-controls="sidebar"><Icon name="sidebar" /></ShellButton><span id="page-title">New session</span><div id="header-actions" className="header-right"><span className="environment">Your team's agent</span></div></header>{children}</div>
    <Toaster position="bottom-center" duration={5500} visibleToasts={1} />
  </TooltipProvider>
}
