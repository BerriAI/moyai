import type { Role } from "../../legacy/bridge"
import { settingsIcons, visibleTo, type SettingsGroup } from "../../config/settings-navigation"

export function SettingsOverview({ groups, role }: { groups: readonly SettingsGroup[]; role: Role }) {
  const visible = visibleTo(role)
  return <div className="settings-grid">{groups.filter(visible).map(group => (
    <section className="settings-group" aria-labelledby={`settings-${group.id}`} key={group.id}>
      <h2 id={`settings-${group.id}`}>{group.title}{group.admin && <span>Admin</span>}</h2>
      <div className="settings-links">{group.items.filter(visible).map(item => {
        const Icon = settingsIcons[item.view]
        return <a key={item.view} className="settings-link" href={`#${item.view}`}>
          <span className="settings-icon">{Icon && <Icon size={24} strokeWidth={1.6} />}</span>
          <span className="settings-copy"><strong>{item.title}</strong><span>{item.description}</span></span>
          <span className="settings-arrow" aria-hidden="true">›</span>
        </a>
      })}</div>
    </section>
  ))}</div>
}
