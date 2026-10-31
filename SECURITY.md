# Security

## Reporting a vulnerability

Please don't open a public issue. Report it privately through [GitHub's vulnerability reporting](https://github.com/cjkas/ESPSomfy-RTS-HA/security/advisories/new) and give the integration and firmware versions, the Home Assistant version, and the steps to reproduce it.

Fixes go into the latest release only, so check that the problem is still there in it.

A problem in the device itself belongs to the [firmware repo](https://github.com/cjkas/ESPSomfy-RTS/security/advisories/new).

## What the integration runs

The integration has no Python requirements of its own (`"requirements": []` in [manifest.json](custom_components/espsomfy_rts/manifest.json)). It only runs its own code and Home Assistant's, and talks to the device on the local network.

## Checking an install

Each release after 3.0.3 has `espsomfy_rts.zip`, the integration's files, with a signed attestation that this repo's [release workflow](.github/workflows/release.yaml) made it from the release's commit. Check it with the [GitHub CLI](https://cli.github.com/):

```
gh attestation verify espsomfy_rts.zip --repo cjkas/ESPSomfy-RTS-HA
```

HACS installs the same files from the release's tag, so you can also compare an install in `custom_components/espsomfy_rts` with the zip's contents.

## How the repo is kept clean

- Every action in the workflows is pinned to a commit. Two of them, hassfest and the HACS validation, run a container image their owners update, so those two checks follow upstream whatever the pin. Dependabot proposes updates to the actions and the test requirements after a week's cooldown.
- The workflows can only read the repo's contents, except the release job, which adds the zip. The scanning jobs can also report to the Security tab.
- [CodeQL](.github/workflows/codeql.yaml) scans the Python and the workflows on every PR, ruff's security rules check the integration, [zizmor](.github/workflows/zizmor.yaml) lints the workflows, and [OpenSSF Scorecard](.github/workflows/scorecard.yaml) rates the repo's practices. Findings are on the repo's Security tab.
