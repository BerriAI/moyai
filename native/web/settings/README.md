# Settings

`settings.tsx` owns navigation between settings pages, their capability-aware
forms and saved-state refresh. `product-gitlab-settings.tsx` owns the GitLab
connection form; `general-preferences.ts` derives the preferences used by the
composer from generated contracts.

Settings composes the independent `analytics` and `automations` pages. Neither
imports Settings. The application imports `SettingsPage` lazily from its file;
there is no eager package entrypoint. Shared transport and controls cannot
depend on Settings. `tests/settings` checks these imports and the existing form
interfaces. Browser coverage remains in `tests/browser`.

`settings.css` is also used by the existing in-conversation credential request.
That style dependency does not load Settings JavaScript or add a second editor.
