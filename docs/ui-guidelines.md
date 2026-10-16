# UI guidelines: adding a feature without moving things people know

Fabric Desktop is used by people who learned where things are. New features go where those people already look. These rules keep it that way; `app/ui/features.js` enforces the first two.

## Where a new page goes

- The left rail holds at most 8 pages (`RAIL_LIMIT`). Today: Home, Create, Workspaces, OneLake, Metadata, Fabric, Monitor, with Settings at the bottom.
- A new feature becomes a **tab or group inside an existing page**. Metadata's tabs are in four groups (Design, Runs, Quality, Deploy, `META_TAB_GROUPS` in `meta.js`); a new metadata feature joins a group, not a fifth row.
- The rail grows only for a new kind of work, and only after a mockup is agreed.
- Settings for a feature go on the Settings page (`settings.js`), in an existing section where one fits.

## Registering a feature

Every page registers itself once:

```js
registerFeature({
  id: "monitor",
  rail: { label: "Monitor", icon: "monitor", order: 70 },   // leave out for pages opened from elsewhere
  views: { monitor: renderMonitor },                          // view id -> render function
  commands: [                                                 // found by the command bar (Ctrl+K)
    { label: "Cost", icon: "gauge", keywords: "capacity cu", preview: true, run: () => { ... } },
  ],
});
```

Register in the file that defines the page's render function, at the end of it.

## Preview features

A feature that hasn't been tried on a real Fabric tenant is in preview:

1. Add it to `PREVIEW_FEATURES` in `features.js`.
2. Wrap its tab or button: `preview("cost", button)`. It gets a Preview tag, or disappears when someone turns preview features off in Settings.
3. Mark its commands `preview: true`.

When it has been tried on a tenant, remove it from `PREVIEW_FEATURES`. The tag goes away everywhere at once.

## Words and help

- Plain words people use, not internal names. "Run in Fabric", not "submit job".
- Every page header ends with `learnMore("<anchor>")`, linking to its section of [user-guide.md](user-guide.md). Add the section in the same pull request.
- Errors go through `friendlyError` (`activity.js`), which turns known causes into a sentence and says what to do.

## Releases

Merges to `main` go to the **Preview** channel (the `latest` release) straight away. People on **Stable**, the default, only get a build when someone runs the **Promote to stable** workflow, whose notes become What's new. Promote after a build has been tried on Preview.
