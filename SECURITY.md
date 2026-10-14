# Security

## Reporting a problem

Please report security problems privately, not in a public issue: on GitHub, open the
**Security** tab of this repository and choose **Report a vulnerability**. You'll get a reply as
soon as I can manage; this is a one-person lab project, so please allow a few days.

## What the app does and doesn't do

- It runs on your own computer and talks to the meters over USB serial. It doesn't listen on any
  network port.
- The only thing it sends over the internet is the optional **Discord alert**, to the webhook URL
  you enter.
- **Webhook URLs are secrets.** Anyone who has one can post to that Discord channel. They're
  stored in plain text in `config.json`, which is gitignored; don't commit it or share it. If a
  webhook URL leaks, delete the webhook in Discord and create a new one.
- Readings and exports stay in your data folder (see the README). Nothing is uploaded.

## Checks

CI scans the whole git history for secrets with [gitleaks](https://github.com/gitleaks/gitleaks)
on every push.
