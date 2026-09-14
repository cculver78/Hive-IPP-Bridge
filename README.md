# Hive IPP Bridge

Hive IPP Bridge is an independent, unofficial Linux compatibility client that
submits local IPP print jobs to PaperCut Hive. It is not affiliated with,
endorsed by, or supported by PaperCut Software.

Developed and maintained by [Edge Case Software](https://edgecasesoftware.dev).

This distribution project is being derived from a working local prototype. It is
not ready for installation yet. The first supported package target will be Arch
Linux/CachyOS, followed by distribution-neutral packaging once enrollment and
credential lifecycle handling are complete.

The project code is released under the [MIT License](LICENSE). The license does not
grant rights to PaperCut trademarks, logos, extension code, or proprietary assets.

## Command-line usage

```text
hive-ipp-bridge setup
hive-ipp-bridge status
hive-ipp-bridge test document.pdf
hive-ipp-bridge uninstall
```

`setup` saves client credentials in a Secret Service-compatible desktop keyring,
starts a user-level IPP printer, and creates one idempotent CUPS queue. This is
not tied to a particular desktop environment: GNOME Keyring, KDE's Secret Service
provider, or another compatible provider can supply the keyring. On lightweight,
custom, or headless sessions, the user must install and start a compatible provider
on the user D-Bus session. Plaintext credential-file fallback is intentionally not
supported.

On first setup, the command prompts for the PaperCut Hive setup link, enrolls
with PaperCut, generates a unique client ID, and stores the resulting
credentials. Existing prototype installations can instead provide
`PAPERCUT_HIVE_JWT`, `PAPERCUT_HIVE_CLIENT_ID`, and `PAPERCUT_HIVE_ORG_ID`
together for migration. Later setup runs do not need the link or variables.

`status` checks the credentials, user service, and CUPS queue. `test` submits the
specified PDF directly and reports whether PaperCut Hive accepted it. `uninstall`
removes the per-user service and queue while preserving credentials by default;
use `hive-ipp-bridge uninstall --purge` to remove the credentials as well.

To remove everything installed for the user, run:

```bash
hive-ipp-bridge uninstall --full
```

Full removal also deletes the installed Python files and the managed
`~/.local/bin/hive-ipp-bridge` launcher.

For upgrades from the working prototype, uninstall also recognizes the legacy
`PaperCut_Hive` queue and removes it only when it uses the expected bridge URI.

## First-time installation

After downloading and extracting a release, run its top-level installer:

```bash
./install.sh
```

This single command installs `hive-ipp-bridge` in `~/.local/bin` and immediately
starts interactive setup. No root access or Python package manager is required.
Once setup completes, a CUPS printer named **Hive_IPP_Bridge** appears in print
dialogs. Afterward, use `hive-ipp-bridge setup` to repair or repeat
configuration. Package manager installations will install the command directly
and will not use this bootstrap script.

## Obtaining the PaperCut Hive setup link

During first-time setup, you will be prompted for a setup link. An administrator
sends this to the user via a Classic Invitation email from the PaperCut Hive
admin console:

1. Sign in to the PaperCut Hive admin console (for example,
   `https://hive.papercut.com` or your region's equivalent).
2. Go to **Edge Mesh** and click **Add Edge Nodes**.
3. Under "Let your users do the work", click **Invite a User**.
4. Select the **Email** tab and enter the user's email address.
5. Click **Customize user's email invitation**.
6. Change the **User invitation type** to **Send a Classic Invitation**.
7. Click **Send Invite**.

The user receives an email with a green **Get Started** button. Right-click the
button and copy the link. It looks like
`https://hive.papercut.com/setup-instructions?t=eyJ…`.

When running `hive-ipp-bridge setup`, paste the copied link at the prompt.

Each setup link is tied to a specific user. If enrollment fails or the link
expires, send a new Classic Invitation from the admin console and run
`hive-ipp-bridge setup` again.

If you are not a PaperCut Hive administrator, ask your IT administrator to
send you a Classic Invitation email.

See [TRADEMARKS.md](TRADEMARKS.md) and [PROVENANCE.md](PROVENANCE.md) before
publishing or redistributing.
