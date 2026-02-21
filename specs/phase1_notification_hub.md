# Phase 1: Notification Hub & Monitoring Pipeline -- Acceptance Specs

## Spec 1: Critical notification fanout

; A critical issue triggers all three channels: Discord, email, and vault.
GIVEN the notification library is installed and configured with valid credentials.
WHEN a critical notification is sent for a named service.
THEN Discord receives a red-colored embed with the service name and message.
AND an email arrives at the configured recipient list with the service name and message.
AND a markdown file appears in the vault monitoring folder with severity "critical", status "open", and the service name in frontmatter.

## Spec 2: Warning notification fanout

; Warnings go to Discord and the vault but skip email.
GIVEN the notification library is installed and configured with valid credentials.
WHEN a warning notification is sent for a named service.
THEN Discord receives a yellow-colored embed with the service name and message.
AND a markdown file appears in the vault monitoring folder with severity "warning".
AND no email is sent.

## Spec 3: Info notification routing

; Info-level findings are recorded in the vault only.
GIVEN the notification library is installed and configured with valid credentials.
WHEN an info notification is sent for a named service.
THEN a markdown file appears in the vault monitoring folder with severity "info".
AND no Discord message is sent.
AND no email is sent.

## Spec 4: Success notification routing

; Successes get a quick Discord green light only.
GIVEN the notification library is installed and configured with valid credentials.
WHEN a success notification is sent for a named service.
THEN Discord receives a green-colored embed with the service name and message.
AND no email is sent.
AND no vault monitoring file is created.

## Spec 5: Vault write failure is non-fatal

; If the vault path is not writable, other channels still deliver.
GIVEN the notification library is configured but the vault path does not exist or is not writable.
WHEN a critical notification is sent.
THEN Discord receives the message.
AND email is delivered.
AND the vault write failure is logged as a warning, not swallowed silently.
AND the notification call does not raise an exception.

## Spec 6: Discord failure is non-fatal

; If Discord webhook is unreachable, other channels still deliver.
GIVEN the notification library is configured but the Discord webhook URL is invalid or unreachable.
WHEN a critical notification is sent.
THEN email is delivered.
AND a vault monitoring file is created.
AND the Discord failure is logged as a warning, not swallowed silently.

## Spec 7: Email failure is non-fatal

; If Gmail SMTP is unreachable, other channels still deliver.
GIVEN the notification library is configured but Gmail SMTP credentials are missing or invalid.
WHEN a critical notification is sent.
THEN Discord receives the message.
AND a vault monitoring file is created.
AND the email failure is logged as a warning, not swallowed silently.

## Spec 8: Vault monitoring file format

; Monitoring files follow a consistent template for Obsidian consumption.
GIVEN a notification is sent with service="bid-scraper", severity="warning", observation="No new records in 48 hours".
WHEN the vault monitoring file is created.
THEN the filename matches the pattern "{service-name}_{YYYY-MM-DD}_{slug}.md".
AND the frontmatter contains: source, service, severity, date, and status="open".
AND the body contains sections: Observation, Expected Behavior, Evidence, Suggested Action.

## Spec 9: All configured channels missing

; If no channels are configured at all, the library logs warnings but does not crash.
GIVEN the notification library is installed with no Discord webhook, no email credentials, and no vault path.
WHEN a notification is sent at any severity.
THEN the call completes without raising an exception.
AND a warning is logged for each unconfigured channel that the severity level would have used.
