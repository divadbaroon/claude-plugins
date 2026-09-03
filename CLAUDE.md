## Cross-repository end-to-end gate

Before any merge, run the native installed round trip from the `berkeley-research`
checkout:

```sh
npm ci
npx playwright install chromium
npm run test:e2e:round-trip
```

Keep `claude-plugins` beside `berkeley-research`, or set
`CLAUDE_PLUGINS_DIR` to its checkout. This test must use the real Engelbart
installer, vendored wheel, installed Claude hook, and loopback UI; do not replace
those boundaries with source imports or mocks. The temporary machine must remain
isolated from the user's real Claude and Engelbart directories.

Do not merge until the cross-repository GitHub Actions jobs pass on macOS,
Ubuntu, and Windows. Changes to the setup/browser handoff must also pass
`npm run test:e2e:compat` (Firefox and WebKit). For coordinated changes, run
each workflow manually with the other repository's branch or SHA before merge.
